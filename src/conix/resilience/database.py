"""T1-D1: bounded fresh-session recovery after qualified PostgreSQL transport loss.

The previous original Tier-0 application and writer must fully retire before
this opt-in supervisor considers another attempt. The journal, source
admission, SQL mutation and reader remain the original implementations.
Unconfirmed COMMIT outcomes are NOT disconnection-restart authority.
"""

import asyncio

import psycopg

from conix.journal import StorageUnavailable
from conix.resilience.reorg import supervise_with_reorg_recovery


# Admit only explicit PostgreSQL connection/shutdown SQLSTATEs. A missing
# SQLSTATE is NOT transport evidence: even real wrong-password authentication
# failures can arrive as generic psycopg.OperationalError with sqlstate=None.
# Unknown transaction resolution (08007), protocol violation (08P01),
# authentication/permission/schema errors and other unclassified failures
# must not grant another application session.
_DISCONNECT_SQLSTATES = frozenset({
    "08000", "08001", "08003", "08006",
    "57P01", "57P02", "57P03",
})

_RESTART_DELAYS = (0.1, 0.2)


def is_restartable_database_failure(failure):
    """Admit only a PostgreSQL transport failure, not a broad storage error."""
    if not isinstance(failure, StorageUnavailable):
        return False
    cause = failure.__cause__
    if not isinstance(cause, psycopg.OperationalError):
        return False
    return cause.sqlstate in _DISCONNECT_SQLSTATES


async def supervise_with_database_recovery(catalog, database_dsn, stop):
    """Restart only after a prior T1-C/T1-B application has fully retired.

    A reconnect is a NEW complete application invocation. It reopens and
    source-checks the existing PostgreSQL journal, reacquires writer exclusion,
    and delegates every block to the original Tier-0 scanner and publisher.
    An interrupted SQL mutation without a sent COMMIT cannot publish; if a
    COMMIT outcome is unknown the journal raises CommitOutcomeUnknown instead,
    which this supervisor never catches or retries.

    There are at most two fresh retries, with cancellable bounded waits.
    A stop cannot convert an unresolved failure into apparent success.
    """
    for failure_count in range(len(_RESTART_DELAYS) + 1):
        if stop.is_set():
            return
        try:
            await supervise_with_reorg_recovery(catalog, database_dsn, stop)
            return
        except StorageUnavailable as failure:
            # The entire original T1-C invocation and all its core session
            # context managers have already exited before reaching here.
            # Any failed retirement overriding the original error propagates.
            if (
                not is_restartable_database_failure(failure)
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

            if stop.is_set():
                raise
