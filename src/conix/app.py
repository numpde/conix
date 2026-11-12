"""The normal standalone Tier-0 indexing loop.

Errors intentionally propagate: the application does not retry failed
operations or decide which retained history to repair. Each advance uses
the installed core's checked one-block journal transaction.
"""

import asyncio
import json

from conix.core import open_writer


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

async def run(catalog, database_dsn, stop, *, poll_interval=2.0):
    """Fresh core writer for EACH block, including independent tip checks.

    A source/SQL/COMMIT failure terminates the process; there is no
    automatic session recovery and no separate validating scanner.
    """
    while not stop.is_set():
        async with asyncio.timeout(catalog.limits.operation_timeout_seconds):
            async with open_writer(
                catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
            ) as writer:
                progress = await writer.advance_one()
        if progress is None:
            await wait_at_tip(stop, poll_interval)
        else:
            report(progress)
