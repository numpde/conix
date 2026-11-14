"""Bounded recovery policy over the original journal's publication observation.

The original application still fails fast. This opt-in composition never
replays SQL or decides durable equivalence: it delegates that comparison to
a fresh exclusive Tier-0 journal before starting the same application again.
"""

import asyncio

from conix.app import RunOutcome
from conix.journal import PublicationObservation, PublicationOutcomeUnknown, open_journal
from conix.resilience.database import supervise_with_database_recovery


_RECOVERY_DELAYS = (0.1, 0.2)


async def supervise_with_publication_recovery(catalog, database_dsn, stop):
    """Permit two recoveries only after exact core-owned effect observations.

    This budget surrounds all nested supervisors and survives their restarts.
    Prior application retirement, inspection, inspection retirement and fresh
    application entry are sequential. Repair uncertainty, failed observation,
    cancellation and cleanup failures cannot authorize another invocation.
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

            async with asyncio.timeout(catalog.limits.operation_timeout_seconds):
                async with open_journal(catalog=catalog, database_dsn=database_dsn) as journal:
                    observation = await journal.reconcile_publication(failure.attempt)
            if observation not in (
                PublicationObservation.ORIGINAL_BASIS_PRESERVED,
                PublicationObservation.EXPECTED_EFFECT_PRESENT,
            ):
                raise
            if stop.is_set():
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
