"""T1-B: repeat the SAME Tier-0 application only after retired sessions.

The original app.run owns the entire catch-up/tip loop and all journal and
RPC lifetimes. T1-B only chooses whether to open a new ordinary session after
a classified connection/read failure. No SQL writes, scanning or repair here.
"""

import asyncio

from conix.resilience.rpc_retry import (
    is_restartable_rpc_failure,
    run_with_read_retries,
)
from conix.rpc import RpcUnavailable


# Fixed, bounded T1-B session policy. A failed read may be attempted three
# times by T1-A inside one original operation; at most two NEW sessions follow
# a separately classified connection failure after full application teardown.
_RESTART_DELAYS = (0.1, 0.2)


async def supervise(catalog, database_dsn, stop):
    """Run the original application, starting a fresh session only when safe."""
    for failure_count in range(len(_RESTART_DELAYS) + 1):
        if stop.is_set():
            return
        try:
            await run_with_read_retries(catalog, database_dsn, stop)
            return
        except RpcUnavailable as failure:
            # Returning from run_with_read_retries means its app.run and
            # open_writer async context have fully exited. Any exception
            # from journal/transport retirement itself propagates rather
            # than being accepted as a safe restart reason.
            if (
                not is_restartable_rpc_failure(failure)
                or failure_count >= len(_RESTART_DELAYS)
                or stop.is_set()
            ):
                raise

            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=_RESTART_DELAYS[failure_count],
                )
            except TimeoutError:
                pass

            # A stop requested during the wait must not turn a failed
            # session into success or grant another application instance.
            if stop.is_set():
                raise
