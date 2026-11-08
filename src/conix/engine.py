"""Tier-0 execution path; no extension hooks or recovery branches."""

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
    await writer.invalidate(basis, message)
    raise BranchConflict(message)


async def advance_one(writer, chain: Chain) -> AdvanceResult:
    """Validate and publish exactly one complete next block, or validate the tip."""
    basis = await writer.basis()
    if basis.blocked_reason is not None:
        raise Blocked(f"Source history is blocked: {basis.blocked_reason}; run conix repair or rebuild.")
    await chain.assert_source()
    head = await chain.current_head()
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
    if tuple(sorted(events, key=lambda e: (e.transaction_index, e.log_index))) != events:
        raise ObservationProtocolError("Collection events are not in transaction/log order.")
    # These are fresh RPC checks, not prefetch/cacheable evidence.
    if not await chain.canonical(block):
        raise ObservationUnavailable("Candidate block changed branch before publication; retry from durable progress.")
    if previous is not None and not await chain.canonical(previous):
        await _invalidate(writer, basis, f"Completed checkpoint {previous.number} changed during acquisition.")
    await writer.publish_block(basis, block, events, head, datetime.now(UTC))
    return AdvanceResult(block.number, True)


async def run_session(journal, chain: Chain, stop: asyncio.Event):
    """One real writer session; errors terminate rather than invoke hidden retry."""
    async with journal.writer() as writer:
        basis = await writer.basis()
        if basis.blocked_reason:
            raise Blocked(f"Indexed history is blocked: {basis.blocked_reason}")
        await chain.assert_source()
        if basis.checkpoint is not None and not await chain.canonical(basis.checkpoint):
            await _invalidate(
                writer, basis,
                f"Stored checkpoint at block {basis.checkpoint.number} is no longer canonical.",
            )
        while not stop.is_set():
            # Bound the entire one-block operation, not only each individual
            # RPC request. Cancellation still unwinds the owned writer session.
            async with asyncio.timeout(chain.settings.operation_timeout_seconds):
                result = await advance_one(writer, chain)
            if not result.changed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=chain.settings.poll_seconds)
                except TimeoutError:
                    pass
