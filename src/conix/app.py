"""The normal standalone Tier-0 indexing loop.

Errors intentionally propagate: the application does not retry failed
operations or decide which retained history to repair. Each advance uses
the installed core's checked one-block journal transaction.
"""

import asyncio
import json
from enum import Enum

from conix.core import open_writer


class RunOutcome(Enum):
    """Distinguish a stopped invocation from one that entered an advance."""

    STOPPED_BEFORE_ADVANCE = "stopped_before_advance"
    STOPPED_AFTER_ADVANCE = "stopped_after_advance"


def report(progress):
    block = progress.last_block
    print(json.dumps({
        "outcome": "advanced",
        "source_id": progress.source_id.hex(),
        "block_number": block.number,
        "block_hash": "0x" + block.hash.hex(),
        "revision": progress.revision,
    }), flush=True)


async def wait_at_tip(stop, interval):
    try:
        await asyncio.wait_for(stop.wait(), interval)
    except TimeoutError:
        pass

async def run(catalog, database_dsn, stop, *, poll_interval=2.0, writer_session=None):
    """Reuse the exclusive core writer while catching up; release at tip."""
    session = open_writer if writer_session is None else writer_session
    outcome = RunOutcome.STOPPED_BEFORE_ADVANCE
    while not stop.is_set():
        async with session(
            catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
        ) as writer:
            while not stop.is_set():
                async with asyncio.timeout(catalog.limits.operation_timeout_seconds):
                    outcome = RunOutcome.STOPPED_AFTER_ADVANCE
                    progress = await writer.advance_one()
                if progress is None:
                    break
                report(progress)
        # Never hold the writer lock while idle at the checked chain tip.
        if not stop.is_set():
            await wait_at_tip(stop, poll_interval)
    return outcome
