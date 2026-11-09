"""Guard the existing four-operation Observer API with durable, canonical evidence.

Each observation is anchored to a complete retained prefix. This module
neither reimplements the scanner nor turns chain facts into accounting state.
"""

import asyncio
from datetime import UTC, datetime
from functools import wraps

from eth_typing import ChecksumAddress

from conix.chain import Chain
from conix.journal import Journal
from conix.models import (
    CanonicalEvidence,
    Checkpoint,
    CollectionCursor,
    CollectionPage,
    HistoryUnavailable,
    ObservationUnavailable,
    PositionObservation,
)


def _operation_deadline(method):
    """Bound each observation without intercepting external task cancellation."""
    @wraps(method)
    async def guarded_call(self, *args, **kwargs):
        try:
            async with asyncio.timeout(self.journal.settings.operation_timeout_seconds):
                return await method(self, *args, **kwargs)
        except TimeoutError as error:
            raise ObservationUnavailable(
                f"Cannot {method.__name__}: observation exceeded its configured operation deadline."
            ) from error

    return guarded_call


class Observer:
    """The concrete Tier-0 observation guard; consumers must not bypass it."""

    def __init__(self, journal: Journal, chain: Chain):
        if journal.settings != chain.settings:
            raise ValueError("Observer's journal and chain must use identical source configuration.")

        self.journal = journal
        self.chain = chain

    async def _checkpoint(self) -> Checkpoint:
        """Choose a complete, currently canonical prefix; reject retired generations."""
        initial_basis = await self.journal.basis()

        if initial_basis.blocked_reason:
            raise ObservationUnavailable(f"Indexed history blocked: {initial_basis.blocked_reason}")

        if initial_basis.checkpoint is None:
            raise ObservationUnavailable("Indexed history has no complete block; run the conix worker.")

        await self.chain.assert_source()

        if not await self.chain.canonical(initial_basis.checkpoint):
            raise ObservationUnavailable("Stored checkpoint is no longer canonical; stop the worker and repair indexed history.")

        upstream_head = await self.chain.current_head()

        if upstream_head.number < initial_basis.checkpoint.number:
            raise ObservationUnavailable("Upstream head is behind the retained checkpoint.")

        # RPC cannot participate in the PostgreSQL snapshot. Reread progress
        # after RPC to exclude a concurrently invalidated or rewound generation.
        current_basis = await self.journal.basis()

        if current_basis.blocked_reason or current_basis.generation != initial_basis.generation:
            raise ObservationUnavailable("Indexed history was invalidated during observation.")

        if current_basis.checkpoint is None or current_basis.checkpoint.number < initial_basis.checkpoint.number:
            raise ObservationUnavailable("Indexed checkpoint disappeared during observation.")

        return Checkpoint(
            deployment=self.journal.settings.deployment,
            generation=initial_basis.generation,
            block=initial_basis.checkpoint,
            collection_count=initial_basis.collection_count,
            upstream_head=upstream_head,
        )

    @_operation_deadline
    async def latest_checkpoint(self) -> Checkpoint:
        return await self._checkpoint()

    @_operation_deadline
    async def latest_position(self, customer: ChecksumAddress, channel_id: int) -> PositionObservation:
        checkpoint = await self._checkpoint()

        # Pin contract reads to the selected block, never an independently
        # changing 'latest' state.
        (asset_address, contract_position) = await self.chain.contract.position(customer, channel_id, checkpoint.block)

        if not await self.chain.canonical(checkpoint.block):
            raise ObservationUnavailable("Position block was replaced during its block-pinned read.")

        await self.journal.ensure_checkpoint(checkpoint)

        return PositionObservation(
            checkpoint=checkpoint,
            observed_at=datetime.now(UTC),
            asset=asset_address,
            position=contract_position,
        )

    @_operation_deadline
    async def voucher_collected_events(
        self, checkpoint: Checkpoint, *, cursor: CollectionCursor | None, limit: int,
    ) -> CollectionPage:
        """Return a complete prefix page only while its generation remains usable."""
        collection_page = await self.journal.page(checkpoint, cursor, limit)

        if not await self.chain.canonical(checkpoint.block):
            raise HistoryUnavailable("Collection checkpoint changed canonical branch; resynchronize.")

        await self.journal.ensure_checkpoint(checkpoint)

        return collection_page

    @_operation_deadline
    async def canonical_evidence(
        self, checkpoint: Checkpoint, block_numbers: tuple[int, ...], *, include_finalized: bool,
    ) -> CanonicalEvidence:
        """Match retained block facts against the source and optional finalized ancestry."""
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

        # Retained intermediate links were checked at publication; load only
        # requested headers. Earlier history needs new upstream RPC evidence.
        retained_start_block = self.journal.settings.start_block
        requested_numbers = {*block_numbers, checkpoint.block.number}
        retained_numbers = tuple(sorted(number for number in requested_numbers if number >= retained_start_block))
        retained_headers = await self.journal.headers_at(checkpoint, retained_numbers)
        headers_by_number = dict(zip(retained_numbers, retained_headers))

        earliest_requested_number = min(block_numbers, default=checkpoint.block.number)

        if earliest_requested_number < retained_start_block:
            if retained_start_block - earliest_requested_number > self.journal.settings.max_evidence_blocks:
                raise ObservationUnavailable("Earlier receipt ancestry exceeds the evidence work bound.")

            earlier_headers = tuple([
                await self.chain.header(number) for number in range(earliest_requested_number, retained_start_block)
            ])
            opening_block = (await self.journal.headers_at(checkpoint, (retained_start_block,)))[0]

            if earlier_headers[-1].hash != opening_block.parent_hash or any(
                following.parent_hash != preceding.hash
                for (preceding, following) in zip(earlier_headers, earlier_headers[1:])
            ):
                raise ObservationUnavailable("Earlier canonical headers do not join retained history.")

            headers_by_number.update((block.number, block) for block in earlier_headers)

        # Locally connected history is not proof that the upstream branch has
        # retained those same identities. Recheck each requested block.
        for block_number in (*block_numbers, checkpoint.block.number):
            canonical_header = await self.chain.header(block_number)

            if canonical_header != headers_by_number[block_number]:
                raise ObservationUnavailable(f"Stored/requested block {block_number} disagrees with the canonical source.")

        finalized_block = None

        if include_finalized:
            finalized_block = await self.chain.finalized()

            if finalized_block.number >= checkpoint.block.number:
                await self.chain.connect(checkpoint.block, finalized_block)
            else:
                recorded_finalized_block = headers_by_number.get(finalized_block.number)

                if recorded_finalized_block is None and finalized_block.number >= retained_start_block:
                    recorded_finalized_block = (await self.journal.headers_at(checkpoint, (finalized_block.number,)))[0]

                if recorded_finalized_block is None and finalized_block.number < retained_start_block:
                    if retained_start_block - finalized_block.number > self.journal.settings.max_evidence_blocks:
                        raise ObservationUnavailable("Finality ancestry preceding coverage exceeds the evidence bound.")

                    earlier_headers = tuple([
                        await self.chain.header(number)
                        for number in range(finalized_block.number, retained_start_block)
                    ])
                    opening_block = (await self.journal.headers_at(checkpoint, (retained_start_block,)))[0]

                    if earlier_headers[-1].hash != opening_block.parent_hash or any(
                        following.parent_hash != preceding.hash
                        for (preceding, following) in zip(earlier_headers, earlier_headers[1:])
                    ):
                        raise ObservationUnavailable("Finalized ancestry does not join retained history.")

                    recorded_finalized_block = earlier_headers[0]

                if recorded_finalized_block != finalized_block:
                    raise ObservationUnavailable("Finalized block disagrees with the checkpoint's canonical ancestry.")

        if not await self.chain.canonical(checkpoint.block):
            raise ObservationUnavailable("Canonical branch changed while collecting confidence evidence.")

        await self.journal.ensure_checkpoint(checkpoint)

        return CanonicalEvidence(
            checkpoint=checkpoint,
            blocks=tuple(headers_by_number[number] for number in block_numbers),
            finalized=finalized_block,
        )
