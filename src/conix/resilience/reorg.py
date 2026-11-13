"""T1-C: bounded canonical-ancestor selection and opt-in checked recovery.

The ordinary Tier-0 application detects contradictions, and its original
writer/journal own every history mutation. Tier 1 only selects a candidate,
verifies that a repair remains necessary, invokes the SAME checked Tier-0
repair, then starts a fresh ordinary supervised application session.
"""

from dataclasses import dataclass
import time

from conix.core import BranchConflict, open_writer
from conix.journal import Block, HistoryConflict, Progress, REORGANIZATION_BLOCKED_REASON
from conix.resilience import supervisor
from conix.rpc import RpcUnavailable


@dataclass(frozen=True, slots=True)
class AncestorSelection:
    """The chosen original retained identity, bound to its inspected SQL basis."""

    basis: Progress
    ancestor: Block


async def select_repair_ancestor(writer) -> AncestorSelection | None:
    """Choose the highest matching retained header under an exclusive writer.

    The caller holds the original open_writer() context throughout this
    operation. Its journal provides source-bound ancestry under the existing
    advisory lock; its _block() performs the SAME RPC/header admission as
    ordinary ingestion. Missing headers are unavailable evidence, not proof
    of a fork. One operation deadline bounds all header reads.

    A canonical retained tip needs no repair (None). Otherwise return a
    candidate with the inspected durable basis. Selection performs NO SQL
    mutation, and is not permission to skip the core's fresh canonicality
    check in Writer.repair_to() when repair is subsequently invoked.
    """
    ancestry = await writer.journal.retained_ancestry(
        limit=writer.catalog.limits.max_evidence_blocks,
    )
    if not ancestry.blocks:
        raise HistoryConflict("No retained ancestor can be selected from an empty history.")

    deadline = time.monotonic() + writer.catalog.limits.operation_timeout_seconds
    ancestor = None
    for retained in ancestry.blocks:
        observed = await writer._block(retained.number, deadline)
        if observed is None:
            raise RpcUnavailable("Selected RPC did not provide a required canonical ancestor header.")
        if observed.header == retained:
            ancestor = retained
            break

    # The journal lock excludes other Conix writers. This final inspection
    # also rejects an unauthorized/out-of-band SQL basis change during reads.
    if await writer.journal.inspect() != ancestry.progress:
        raise HistoryConflict("Durable history changed during recovery-ancestor selection.")

    if ancestor is None:
        raise BranchConflict("No canonical retained ancestor was established within the evidence window.")
    if ancestor == ancestry.progress.last_block:
        return None
    return AncestorSelection(basis=ancestry.progress, ancestor=ancestor)


async def recover_reorganization(catalog, database_dsn, stop, *, blocked_only=False):
    """Revalidate and optionally repair one selected fork in a fresh writer.

    Return True only after the original checked repair commits, False when
    no repair is needed, and None when recovery is not authorized or stopped.
    The caller must first retire the failed application session. No recovery
    is authorized from an unrelated HistoryConflict without a persisted
    blocked basis, or from selection alone.

    The ancestor and stored tip are read AGAIN after selection. This narrows
    the interval before mutation, but the external RPC branch cannot be
    frozen across a PostgreSQL transaction; Writer.repair_to independently
    checks the ancestor again before its guarded journal transaction.
    """
    if stop.is_set():
        return None

    async with open_writer(
        catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
    ) as writer:
        reason = writer.journal.basis.blocked_reason
        if reason not in (None, REORGANIZATION_BLOCKED_REASON):
            return None
        if blocked_only and reason != REORGANIZATION_BLOCKED_REASON:
            return None

        selection = await select_repair_ancestor(writer)
        if stop.is_set():
            return None
        if selection is None:
            return False

        deadline = time.monotonic() + catalog.limits.operation_timeout_seconds

        # Selection is not a mutation grant. Revalidate its claimed ancestor
        # against the live provider before rechecking whether the old stored
        # tip still conflicts. A missing block is unavailable evidence.
        ancestor = await writer._block(selection.ancestor.number, deadline)
        if ancestor is None:
            raise RpcUnavailable("Canonical repair ancestor is unavailable on the selected RPC.")
        if ancestor.header != selection.ancestor:
            raise BranchConflict("Selected repair ancestor changed before mutation.")

        tip = await writer._block(selection.basis.last_block.number, deadline)
        if tip is None:
            raise RpcUnavailable("Canonical tip evidence is unavailable before repair.")
        if tip.header == selection.basis.last_block:
            return False

        if await writer.journal.inspect() != selection.basis:
            raise HistoryConflict("Durable history changed before checked repair.")
        if stop.is_set():
            return None

        # The SAME Tier-0 core checks the ancestor once more, then performs
        # the ONLY original journal suffix repair under exclusive ownership.
        await writer.repair_to(ancestor=selection.ancestor)
        return True


_RECOVERY_BUDGET = 2


async def supervise_with_reorg_recovery(catalog, database_dsn, stop):
    """Run original T1-B supervision; recover at most two confirmed forks.

    Each failed T1-B call has already retired its complete original app.run,
    writer, and HTTP session before this wrapper can open a recovery writer.
    The fresh session after repair re-reads progress through ordinary Tier 0.
    A persisted blocked basis can recover after process restart, without the
    original in-memory BranchConflict.
    """
    for attempt in range(_RECOVERY_BUDGET + 1):
        if stop.is_set():
            return
        try:
            await supervisor.supervise(catalog, database_dsn, stop)
            return
        except (BranchConflict, HistoryConflict) as failure:
            if stop.is_set() or attempt >= _RECOVERY_BUDGET:
                raise

            recovered = await recover_reorganization(
                catalog, database_dsn, stop,
                blocked_only=isinstance(failure, HistoryConflict),
            )
            if (
                stop.is_set()
                or recovered is None
                or (isinstance(failure, HistoryConflict) and not recovered)
            ):
                raise
