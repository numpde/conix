"""Guarded, database-backed implementation of the existing four-operation Observer API.

No ledger dependency and no second scan algorithm. Every read is anchored to a
durable complete prefix and checked against the configured canonical source.
"""

from datetime import UTC, datetime

from eth_typing import ChecksumAddress

from conix.chain import Chain
from conix.errors import Blocked
from conix.journal import Journal
from conix.models import (
    Block,
    CanonicalEvidence,
    Checkpoint,
    CollectionCursor,
    CollectionPage,
    HistoryUnavailable,
    ObservationProtocolError,
    ObservationUnavailable,
    PositionObservation,
)


class Observer:
    """The concrete Tier-0 observation guard; callers must not bypass it."""

    def __init__(self, journal: Journal, chain: Chain):
        if journal.settings != chain.settings:
            raise ValueError("Observer's journal and chain must use identical source configuration.")
        self.journal = journal
        self.chain = chain

    async def _checkpoint(self) -> Checkpoint:
        state = await self.journal.basis()
        if state.blocked_reason:
            raise ObservationUnavailable(f"Indexed history blocked: {state.blocked_reason}")
        if state.checkpoint is None:
            raise ObservationUnavailable("Indexed history has no complete block; run the conix worker.")
        await self.chain.assert_source()
        if not await self.chain.canonical(state.checkpoint):
            raise ObservationUnavailable(
                "Stored checkpoint is no longer canonical; stop the worker and repair indexed history."
            )
        head = await self.chain.current_head()
        if head.number < state.checkpoint.number:
            raise ObservationUnavailable("Upstream head is behind the retained checkpoint.")
        # Source may have changed while remote requests were underway.
        current = await self.journal.basis()
        if current.blocked_reason or current.generation != state.generation:
            raise ObservationUnavailable("Indexed history was invalidated during observation.")
        if current.checkpoint is None or current.checkpoint.number < state.checkpoint.number:
            raise ObservationUnavailable("Indexed checkpoint disappeared during observation.")
        return Checkpoint(
            self.journal.settings.deployment,
            state.generation,
            state.checkpoint,
            state.collection_count,
            head,
        )

    async def latest_checkpoint(self) -> Checkpoint:
        return await self._checkpoint()

    async def latest_position(self, customer: ChecksumAddress, channel_id: int) -> PositionObservation:
        checkpoint = await self._checkpoint()
        asset, position = await self.chain.contract.position(customer, channel_id, checkpoint.block)
        if not await self.chain.canonical(checkpoint.block):
            raise ObservationUnavailable("Position block was replaced during its block-pinned read.")
        await self.journal.ensure_checkpoint(checkpoint)
        return PositionObservation(checkpoint, datetime.now(UTC), asset, position)

    async def voucher_collected_events(
        self, checkpoint: Checkpoint, *, cursor: CollectionCursor | None, limit: int,
    ) -> CollectionPage:
        page = await self.journal.page(checkpoint, cursor, limit)
        if not await self.chain.canonical(checkpoint.block):
            raise HistoryUnavailable("Collection checkpoint changed canonical branch; resynchronize.")
        await self.journal.ensure_checkpoint(checkpoint)
        return page

    async def canonical_evidence(
        self, checkpoint: Checkpoint, block_numbers: tuple[int, ...], *, include_finalized: bool,
    ) -> CanonicalEvidence:
        if type(checkpoint) is not Checkpoint or checkpoint.deployment != self.journal.settings.deployment:
            raise HistoryUnavailable("Canonical evidence requires a checkpoint from this deployment.")
        if type(include_finalized) is not bool or type(block_numbers) is not tuple:
            raise ValueError("Canonical evidence requires a tuple of block heights and a bool include_finalized.")
        if any(type(number) is not int or number < 0 or number > checkpoint.block.number for number in block_numbers):
            raise ValueError("Canonical evidence heights must be nonnegative and at/below checkpoint.")
        if len(set(block_numbers)) != len(block_numbers):
            raise ValueError("Canonical evidence heights must be unique.")
        if len(block_numbers) > self.journal.settings.max_evidence_blocks:
            raise ObservationUnavailable("Canonical evidence exceeds configured size limit.")
        await self.journal.ensure_checkpoint(checkpoint)
        if not await self.chain.canonical(checkpoint.block):
            raise ObservationUnavailable("Checkpoint is no longer canonical.")

        # Preserve source-backed evidence even for receipts preceding the indexer's
        # configured history start; a gap too large to prove is unavailable.
        first_requested = min(block_numbers, default=checkpoint.block.number)
        start = self.journal.settings.start_block
        if first_requested < start and start - first_requested > self.journal.settings.max_evidence_blocks:
            raise ObservationUnavailable("Earlier receipt ancestry exceeds the evidence work bound.")
        retained_first = max(start, first_requested)
        retained = await self.journal.stored_headers(retained_first, checkpoint.block.number)
        recorded = {block.number: block for block in retained}
        earlier: list[Block] = []
        if first_requested < start:
            for number in range(first_requested, start):
                earlier.append(await self.chain.header(number))
            preceding = earlier[-1]
            if preceding.hash != retained[0].parent_hash:
                raise ObservationUnavailable("Earlier canonical headers do not join retained history.")
            if any(right.parent_hash != left.hash for left, right in zip(earlier, earlier[1:])):
                raise ObservationUnavailable("Earlier canonical headers have broken ancestry.")
        recorded.update((block.number, block) for block in earlier)

        # Check requested canonical identities independently against RPC.
        for number in (*block_numbers, checkpoint.block.number):
            actual = await self.chain.header(number)
            if actual != recorded[number]:
                raise ObservationUnavailable(f"Stored/requested block {number} disagrees with the canonical source.")
        finalized = None
        if include_finalized:
            finalized = await self.chain.finalized()
            if finalized.number >= checkpoint.block.number:
                await self.chain.connect(checkpoint.block, finalized)
            else:
                recorded_finalized = recorded.get(finalized.number)
                if recorded_finalized is None:
                    raise ObservationUnavailable(
                        "Finalized block lies outside the supported retained/earlier ancestry window."
                    )
                if recorded_finalized != finalized:
                    raise ObservationUnavailable("Finalized block disagrees with the checkpoint's canonical ancestry.")
        if not await self.chain.canonical(checkpoint.block):
            raise ObservationUnavailable("Canonical branch changed while collecting confidence evidence.")
        await self.journal.ensure_checkpoint(checkpoint)
        return CanonicalEvidence(checkpoint, tuple(recorded[number] for number in block_numbers), finalized)
