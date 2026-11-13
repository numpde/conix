"""Opt-in Tier-1 database composition: initially delegates without recovery.

The first executable witness records the existing behavior before granting
any new retry authority. Its target is the original complete application.
"""

from conix.resilience.reorg import supervise_with_reorg_recovery


async def supervise_with_database_recovery(catalog, database_dsn, stop):
    """Run the existing supervised application without SQL retry authority."""
    await supervise_with_reorg_recovery(catalog, database_dsn, stop)
