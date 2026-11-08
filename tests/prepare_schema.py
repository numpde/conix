"""One-shot installed-package schema preparation as a distinct test identity.

This subprocess receives disposable owner credentials; the test worker does not.
It exercises exactly the public owner installer, twice, to verify idempotence.
"""

import asyncio
import os

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from conix.schema import initialize
from conix.journal import _state
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


if __name__ == "__main__":
    asyncio.run(main())
