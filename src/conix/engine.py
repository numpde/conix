"""Publish one complete canonical block at a time; never retry or repair implicitly."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from conix.chain import Chain
from conix.errors import Blocked, BranchConflict
from conix.models import ObservationProtocolError, ObservationUnavailable


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    block_number: int | None
    changed: bool


async def _invalidate(writer, basis, message: str):
    """Persist a canonical contradiction before refusing further observations."""
    await writer.invalidate(basis, message)
    raise BranchConflict(message)


async def advance_one(writer, chain: Chain) -> AdvanceResult:
    """Check the upstream tip or commit exactly one complete block."""
    publication_basis = await writer.basis()

    if publication_basis.blocked_reason is not None:
        raise Blocked(f"Source history is blocked: {publication_basis.blocked_reason}; run conix repair or rebuild.")

    await chain.assert_source()
    upstream_head = await chain.current_head()

    # A reorg can replace the completed block without advancing the head.
    completed_block = publication_basis.checkpoint
    if completed_block is not None:
        if upstream_head.number < completed_block.number:
            raise ObservationUnavailable(
                f"RPC head {upstream_head.number} is behind completed checkpoint {completed_block.number}; no progress published."
            )

        observed_checkpoint = await chain.header(completed_block.number)

        if observed_checkpoint.hash != completed_block.hash or observed_checkpoint.parent_hash != completed_block.parent_hash:
            await _invalidate(writer, publication_basis, f"Canonical block changed at completed height {completed_block.number}.")

    next_block_number = chain.settings.start_block if completed_block is None else completed_block.number + 1
    if next_block_number > upstream_head.number:
        await writer.note_head(publication_basis, upstream_head, datetime.now(UTC))
        return AdvanceResult(completed_block.number if completed_block else None, False)

    candidate_block = await chain.header(next_block_number)

    if completed_block is not None:
        if candidate_block.parent_hash != completed_block.hash:
            await _invalidate(writer, publication_basis, f"Next block {candidate_block.number} no longer extends the retained checkpoint.")
    elif next_block_number > 0:
        predecessor = await chain.header(next_block_number - 1)

        if candidate_block.parent_hash != predecessor.hash:
            raise ObservationUnavailable("Initial block is not connected to its canonical predecessor.")

    collected_events = await chain.contract.voucher_collected_events(candidate_block)

    if type(collected_events) is not tuple:
        raise ObservationProtocolError("Decoded collection events must be an immutable tuple.")

    if len({event.log_index for event in collected_events}) != len(collected_events):
        raise ObservationProtocolError("Duplicate log index in one block.")

    if tuple(sorted(collected_events, key=lambda event: (event.transaction_index, event.log_index))) != collected_events:
        raise ObservationProtocolError("Collection events are not in transaction/log order.")

    # Recheck canonicality after acquiring logs, before publishing the block.
    if not await chain.canonical(candidate_block):
        raise ObservationUnavailable("Candidate block changed branch before publication; retry from durable progress.")

    if completed_block is not None and not await chain.canonical(completed_block):
        await _invalidate(writer, publication_basis, f"Completed checkpoint {completed_block.number} changed during acquisition.")

    await writer.publish_block(
        expected=publication_basis,
        block=candidate_block,
        collections=collected_events,
        head=upstream_head,
        now=datetime.now(UTC),
    )

    return AdvanceResult(candidate_block.number, True)


async def run_session(journal, chain: Chain, stop: asyncio.Event):
    """Hold one writer session; failures exit for a fresh durable-state check."""
    async with journal.writer() as writer:
        publication_basis = await writer.basis()

        if publication_basis.blocked_reason:
            raise Blocked(f"Indexed history is blocked: {publication_basis.blocked_reason}")

        await chain.assert_source()
        if publication_basis.checkpoint is not None and not await chain.canonical(publication_basis.checkpoint):
            await _invalidate(
                writer,
                publication_basis,
                f"Stored checkpoint at block {publication_basis.checkpoint.number} is no longer canonical.",
            )

        while not stop.is_set():
            # The deadline covers the whole block operation, not just each RPC;
            # cancellation releases the PostgreSQL writer session.
            async with asyncio.timeout(chain.settings.operation_timeout_seconds):
                advance_result = await advance_one(writer, chain)

            if not advance_result.changed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=chain.settings.poll_seconds)
                except TimeoutError:
                    pass
