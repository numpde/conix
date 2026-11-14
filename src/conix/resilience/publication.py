"""Bounded continuation after journal-originated publication COMMIT uncertainty.

The original application still fails fast, and its journal alone owns SQL
publication and durable progress. This opt-in composition never replays the
uncertain transaction or verifies its old effect as a prerequisite. A fresh
ordinary supervised application reopens the authoritative writer, checks
source and chain identity, and determines the next block from durable history.
"""

import asyncio

from conix.app import RunOutcome
from conix.journal import PublicationOutcomeUnknown
from conix.resilience.database import supervise_with_database_recovery


_RECOVERY_DELAYS = (0.1, 0.2)


async def supervise_with_publication_recovery(catalog, database_dsn, stop):
    """Retry a complete application after publication-specific uncertainty.

    Only the original journal's PublicationOutcomeUnknown is eligible.
    The previous application has completed its writer/HTTP lifetimes before
    the exception reaches this wrapper. Its pending SQL mutation is never
    replayed: the next original application reacquires ownership and reads
    the durable checkpoint. Its ordinary source/chain and operator-hold
    guards remain in force; uncertain repair is not admitted.

    At most two additional D2 invocations follow bounded cancellable waits.
    Existing nested supervisors retain their own budgets. Stop/cancellation
    and failed retirement never authorize another application. A fresh
    invocation that stops before entering advance is not recovery success.
    """
    previous = None
    for recovery_count in range(len(_RECOVERY_DELAYS) + 1):
        if stop.is_set():
            if previous is not None:
                raise previous
            return RunOutcome.STOPPED_BEFORE_ADVANCE

        try:
            outcome = await supervise_with_database_recovery(catalog, database_dsn, stop)
        except PublicationOutcomeUnknown as failure:
            if recovery_count >= len(_RECOVERY_DELAYS) or stop.is_set():
                raise

            try:
                await asyncio.wait_for(stop.wait(), timeout=_RECOVERY_DELAYS[recovery_count])
            except TimeoutError:
                pass
            if stop.is_set():
                raise
            previous = failure
            continue

        # A stop can race with any nested supervisor's entry check. Only
        # the original loop can tell whether an advance actually started.
        if previous is not None and outcome is not RunOutcome.STOPPED_AFTER_ADVANCE:
            raise previous
        return outcome
