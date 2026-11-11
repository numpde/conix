"""Exercise the bounded, source-checked retained ancestry inspection API.

This API is useful to an operator when a checkpoint is no longer canonical.
It reports stored facts, never asserts that a block is still canonical.
"""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import unittest

from psycopg import AsyncConnection

from conix.catalog import prepare_catalog
from conix.journal import (
    Block,
    HistoryConflict,
    WriterBusy,
    inspect_retained_ancestry,
    open_journal,
)


@unittest.skipUnless(os.getenv("CONIX_TEST_DSN"), "Requires the owner-initialized PostgreSQL test source")
class RetainedAncestryContracts(unittest.IsolatedAsyncioTestCase):
    """The same existing journal protects ancestry reads and modifications."""

    async def test_retained_history_is_bounded_and_readable_during_recovery(self):
        """Return actual stored headers even when progress is administratively blocked."""
        catalog = prepare_catalog(Path(__file__).resolve().parent / "fixtures" / "catalog.toml")
        runtime_dsn = os.environ["CONIX_TEST_DSN"]

        async with open_journal(catalog=catalog, database_dsn=runtime_dsn) as journal:
            original = await journal.inspect()
            parent = original.last_block
            appended = []
            for height in range(
                (parent.number + 1) if parent else catalog.chain.start_block,
                ((parent.number + 1) if parent else catalog.chain.start_block) + 2,
            ):
                header = Block(
                    number=height,
                    hash=hashlib.sha256(f"ancestry-probe-{height}".encode()).digest(),
                    parent_hash=parent.hash if parent else bytes(32),
                    timestamp=1700000000 + height,
                )
                await journal.publish_block(block=header, events=())
                appended.append(header)
                parent = header

            # A second writer may not inspect or mutate the retained prefix
            # while this session owns the journal advisory lock.
            with self.assertRaises(WriterBusy):
                await inspect_retained_ancestry(
                    catalog=catalog, database_dsn=runtime_dsn, limit=2,
                )

        observed = await inspect_retained_ancestry(
            catalog=catalog, database_dsn=runtime_dsn, limit=2,
        )
        self.assertEqual(observed.blocks, tuple(reversed(appended)))
        self.assertEqual(observed.progress.last_block, appended[-1])
        self.assertEqual(observed.progress.source_id, catalog.source_id)

        async with await AsyncConnection.connect(runtime_dsn, connect_timeout=5) as connection:
            await connection.execute(
                "UPDATE conix.source_progress SET blocked_reason = %s WHERE singleton = 1",
                ("recovery required",),
            )
        try:
            blocked = await inspect_retained_ancestry(
                catalog=catalog,
                database_dsn=runtime_dsn,
                through=appended[-2].number,
                limit=2,
            )
            self.assertEqual(blocked.progress.blocked_reason, "recovery required")
            self.assertEqual(blocked.blocks[0], appended[-2])
            self.assertTrue(all(block.number <= appended[-2].number for block in blocked.blocks))
        finally:
            async with await AsyncConnection.connect(runtime_dsn, connect_timeout=5) as connection:
                await connection.execute(
                    "UPDATE conix.source_progress SET blocked_reason = NULL WHERE singleton = 1"
                )

        with self.assertRaises(HistoryConflict):
            await inspect_retained_ancestry(
                catalog=replace(catalog, source_id=bytes.fromhex("ab" * 32)),
                database_dsn=runtime_dsn, limit=1,
            )

        for limit in (0, -1, True, catalog.limits.max_evidence_blocks + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await inspect_retained_ancestry(
                    catalog=catalog, database_dsn=runtime_dsn, limit=limit,
                )

        with self.assertRaises(HistoryConflict):
            await inspect_retained_ancestry(
                catalog=catalog, database_dsn=runtime_dsn,
                through=appended[-1].number + 1, limit=1,
            )


if __name__ == "__main__":
    unittest.main()
