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
    """Persist a contradiction before refusing further observations."""
    await writer.invalidate(basis, message)
    raise BranchConflict(message)


async def advance_one(writer, chain: Chain) -> AdvanceResult:
    """Check the tip or commit exactly one next block, including empty blocks."""
    basis = await writer.basis()
    if basis.blocked_reason is not None:
        raise Blocked(f"Source history is blocked: {basis.blocked_reason}; run conix repair or rebuild.")

    await chain.assert_source()
    head = await chain.current_head()

    # Check the last published block even at the tip: a reorg need not advance
    # the head to invalidate already stored history.
    previous = basis.checkpoint
    if previous is not None:
        if head.number < previous.number:
            raise ObservationUnavailable(
                f"RPC head {head.number} is behind completed checkpoint {previous.number}; no progress published."
            )

        observed = await chain.header(previous.number)
        if observed.hash != previous.hash or observed.parent_hash != previous.parent_hash:
            await _invalidate(writer, basis, f"Canonical block changed at completed height {previous.number}.")

    next_height = chain.settings.start_block if previous is None else previous.number + 1
    if next_height > head.number:
        await writer.note_head(basis, head, datetime.now(UTC))
        return AdvanceResult(previous.number if previous else None, False)

    block = await chain.header(next_height)
    if previous is not None:
        if block.parent_hash != previous.hash:
            await _invalidate(writer, basis, f"Next block {block.number} no longer extends the retained checkpoint.")
    elif next_height > 0:
        predecessor = await chain.header(next_height - 1)
        if block.parent_hash != predecessor.hash:
            raise ObservationUnavailable("Initial block is not connected to its canonical predecessor.")

    events = await chain.contract.voucher_collected_events(block)
    if type(events) is not tuple:
        raise ObservationProtocolError("Decoded collection events must be an immutable tuple.")

    if len({event.log_index for event in events}) != len(events):
        raise ObservationProtocolError("Duplicate log index in one block.")

    if tuple(sorted(events, key=lambda event: (event.transaction_index, event.log_index))) != events:
        raise ObservationProtocolError("Collection events are not in transaction/log order.")

    # Recheck after reading logs; earlier header responses do not certify that
    # the branch remained canonical while the block was assembled.
    if not await chain.canonical(block):
        raise ObservationUnavailable("Candidate block changed branch before publication; retry from durable progress.")

    if previous is not None and not await chain.canonical(previous):
        await _invalidate(writer, basis, f"Completed checkpoint {previous.number} changed during acquisition.")

    await writer.publish_block(basis, block, events, head, datetime.now(UTC))
    return AdvanceResult(block.number, True)


async def run_session(journal, chain: Chain, stop: asyncio.Event):
    """Hold one writer session; failures exit for a fresh durable-state check."""
    async with journal.writer() as writer:
        basis = await writer.basis()
        if basis.blocked_reason:
            raise Blocked(f"Indexed history is blocked: {basis.blocked_reason}")

        await chain.assert_source()
        if basis.checkpoint is not None and not await chain.canonical(basis.checkpoint):
            await _invalidate(writer, basis, f"Stored checkpoint at block {basis.checkpoint.number} is no longer canonical.")

        while not stop.is_set():
            # The deadline covers the whole block operation, not just each RPC;
            # cancellation releases the PostgreSQL writer session.
            async with asyncio.timeout(chain.settings.operation_timeout_seconds):
                result = await advance_one(writer, chain)

            if not result.changed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=chain.settings.poll_seconds)
                except TimeoutError:
                    pass
