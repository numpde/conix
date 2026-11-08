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
from conix.schema import initialize
from conix.journal import _state, advisory_key
from test_tier0 import settings


async def main():
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

    # Different source selection must not adopt/overwrite this database.
    try:
        await initialize(owner, replace(source, start_block=source.start_block + 1))
    except ConfigurationError:
        pass
    else:
        raise AssertionError("Owner initialization accepted another source on an occupied database.")

    # The installation lock is exactly the normal Tier-0 writer lock.
    # Another session cannot install DDL while a writer owns the source.
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
