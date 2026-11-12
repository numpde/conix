"""Autonomous Tier-0 indexer with an OPTIONAL private command service.

Variant C uses exactly the same one-block core publisher as variant B.
Command handling only controls scheduling: it cannot provide events,
validate evidence, mutate stored history, or silently retry failures.
"""

import asyncio
import json

from conix.control import Controls, SOCKET, serve
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


async def _wait(controls, stop, timeout):
    """Wake for shutdown, a control change or the next ordinary tip poll."""
    if controls.changed.is_set():
        controls.changed.clear()
        return
    stopped = asyncio.create_task(stop.wait())
    changed = asyncio.create_task(controls.changed.wait())
    try:
        await asyncio.wait(
            (stopped, changed), timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        stopped.cancel()
        changed.cancel()
        await asyncio.gather(stopped, changed, return_exceptions=True)
        controls.changed.clear()


async def run(catalog, database_dsn, stop, *, poll_interval=2.0, socket_path=SOCKET):
    """Catch up and follow tip autonomously; optional local pause/resume/step."""
    controls = Controls()
    async with serve(controls, socket_path):
        while not stop.is_set():
            if controls.paused and not controls.step_requested:
                await _wait(controls, stop, None)
                continue

            at_tip = False
            async with open_writer(
                catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
            ) as writer:
                while not stop.is_set():
                    if controls.paused:
                        if not controls.step_requested:
                            break
                        controls.step_requested = False

                    progress = await writer.advance_one()
                    if progress is None:
                        at_tip = True
                        break
                    controls.last_committed = progress.last_block.number
                    report(progress)

            if not stop.is_set() and at_tip:
                await _wait(controls, stop, poll_interval)
