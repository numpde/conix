"""T1-C1: select a canonical retained ancestor; never perform repair.

The ordinary core detects branch contradictions. This narrow recovery decision
uses its verified writer, canonical header parser and durable ancestry snapshot.
The caller owns the writer's exclusive lifetime and any later repair decision.
"""

from dataclasses import dataclass
import time

from conix.core import BranchConflict
from conix.journal import Block, HistoryConflict, Progress
from conix.rpc import RpcUnavailable


@dataclass(frozen=True, slots=True)
class AncestorSelection:
    """The chosen original retained identity, bound to its inspected SQL basis."""

    basis: Progress
    ancestor: Block


async def select_repair_ancestor(writer) -> AncestorSelection | None:
    """Choose the highest matching retained header under an exclusive writer.

    The caller must hold the original open_writer() context throughout this
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
