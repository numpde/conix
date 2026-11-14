"""Tier-1 bounded retries of the concrete Tier-0 selected RPC reads.

This module never owns HTTP, request encoding, evidence validation, journal
transactions or tip scheduling. Only connection/read transport failures
(and an individual request timeout with time remaining) are retryable.
RPC provider rejections and absent/contradictory evidence are not.
"""

import asyncio
from contextlib import asynccontextmanager
import time

import httpx

from conix import app
from conix.core import Writer, open_writer
from conix.rpc import RpcUnavailable


# The continuous writer uses exactly these independently read-only RPCs.
# Source admission occurs inside open_writer before this decorator is active.
_RETRYABLE_READS = frozenset({"eth_getBlockByNumber", "eth_getLogs"})
_CONNECTION_FAILURE = (
    httpx.ConnectError, httpx.ConnectTimeout,
    httpx.ReadError, httpx.ReadTimeout,
)
_TRANSIENT_TRANSPORT = (*_CONNECTION_FAILURE,
                        TimeoutError)  # Only an individual request timeout.


def is_restartable_rpc_failure(error):
    """Admit only connection/read failures for a NEW complete app session.

    A timed-out individual request is eligible within T1-A's original
    deadline, but the supervisor cannot extend that expired operation. It
    therefore does not restart an application session on TimeoutError.
    Provider rejection and deadline exhaustion have no eligible cause.
    """
    return isinstance(error, RpcUnavailable) and isinstance(
        error.__cause__, _CONNECTION_FAILURE,
    )
_ATTEMPTS = 3
_INITIAL_DELAY = 0.05


class RetryingRPC:
    """Delegate every attempt to the SAME concrete RPC.call implementation."""

    def __init__(self, rpc):
        self.base = rpc

    async def call(self, method, params, *, deadline):
        """Retry only classified read failures within the supplied deadline."""
        for attempt in range(_ATTEMPTS):
            try:
                return await self.base.call(method, params, deadline=deadline)
            except RpcUnavailable as error:
                # A rejected JSON-RPC response, HTTP non-200, unavailable
                # history or expired operation deadline carries no eligible
                # transport cause. No message matching or generic retry.
                if (
                    method not in _RETRYABLE_READS
                    or not isinstance(error.__cause__, _TRANSIENT_TRANSPORT)
                    or attempt == _ATTEMPTS - 1
                ):
                    raise

                delay = _INITIAL_DELAY * (2 ** attempt)
                if time.monotonic() + delay >= deadline:
                    raise

                # Cancellation propagates through the original request or
                # this sleep; no shield, detached task or subsequent attempt.
                await asyncio.sleep(delay)

        raise AssertionError("The bounded retry loop returned no result or exception.")


@asynccontextmanager
async def retrying_writer_session(*, catalog, database_dsn, rpc_url):
    """Use one original writer/journal; decorate only reads after source admission."""
    async with open_writer(
        catalog=catalog, database_dsn=database_dsn, rpc_url=rpc_url,
    ) as selected:
        # Construct the SAME Tier-0 Writer class with the SAME catalog and
        # exclusive journal. This is composition, not monkey-patching the
        # installed writer or providing an alternative publisher.
        yield Writer(
            catalog=selected.catalog,
            journal=selected.journal,
            rpc=RetryingRPC(selected.rpc),
        )


async def run_with_read_retries(catalog, database_dsn, stop, *, poll_interval=2.0):
    """Compose retries into the unchanged Tier-0 catch-up/tip-following loop."""
    return await app.run(
        catalog, database_dsn, stop,
        poll_interval=poll_interval,
        writer_session=retrying_writer_session,
    )
