"""Follow the chain tip by repeating the installed Tier-0 advance."""

import asyncio

from conix.worker import advance


async def follow(catalog, database_dsn, *, stop: asyncio.Event, poll_interval: float = 2.0) -> None:
    """Advance without delaying catch-up; wait only after a verified tip.

    Each Tier-0 invocation owns and closes its writer session. A stop request
    ends the loop after an active invocation or immediately during tip idle.
    Failures propagate: retry, reconciliation, and repair are later Tier-1 work.
    """
    if poll_interval <= 0:
        raise ValueError("Tip polling interval must be positive.")

    while not stop.is_set():
        if await advance(catalog, database_dsn) is not None:
            continue

        try:
            async with asyncio.timeout(poll_interval):
                await stop.wait()
        except TimeoutError:
            pass
