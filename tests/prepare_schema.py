"""One-shot installed-package schema preparation as a distinct test identity.

This subprocess receives disposable owner credentials; the test worker does not.
It exercises exactly the public owner installer, twice, to verify idempotence.
"""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

# PYTHONSAFEPATH deliberately omits the executed script's directory.
# The trusted test fixture lives in the installed image's fixed /tests tree.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from conix.errors import ConfigurationError, WriterBusy
from conix.journal import _state, advisory_key
from conix.schema import initialize
from test_tier0 import settings


async def main():
    """Verify idempotent owner enrollment and exclusion by a live writer."""
    source = settings()
    owner = os.environ["CONIX_TEST_OWNER_DSN"]
    await initialize(owner, source)
    async with await AsyncConnection.connect(
        owner, autocommit=True, row_factory=dict_row
    ) as connection:
        first = await _state(connection, source)
    await initialize(owner, source)
    async with await AsyncConnection.connect(
        owner, autocommit=True, row_factory=dict_row
    ) as connection:
        second = await _state(connection, source)
    if first != second:
        raise AssertionError("Repeated owner initialization changed the durable source or its generation.")

    # An existing source is immutable: a different start height must fail
    # without adopting, resetting or rewriting its durable state.
    try:
        await initialize(owner, replace(source, start_block=source.start_block + 1))
    except ConfigurationError:
        pass
    else:
        raise AssertionError("Owner initialization accepted another source on an occupied database.")

    # Schema installation uses the same advisory lock as the concrete writer.
    # This is tested against PostgreSQL, not a mocked lock implementation.
    async with await AsyncConnection.connect(
        owner, autocommit=True, row_factory=dict_row
    ) as connection:
        key = advisory_key(source)
        observed = await (await connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (key,)
        )).fetchone()
        if not observed["acquired"]:
            raise AssertionError("Test preparation could not acquire its isolated writer lock.")

        try:
            try:
                await initialize(owner, source)
            except WriterBusy:
                pass
            else:
                raise AssertionError("Owner DDL did not respect an active writer's lock.")
        finally:
            await connection.execute("SELECT pg_advisory_unlock(%s)", (key,))


if __name__ == "__main__":
    asyncio.run(main())
