"""Exercise the installed one-block PostgreSQL publisher and checked repair."""

import os
from pathlib import Path
import unittest

from psycopg import AsyncConnection

from abi_golden_vectors import ERC20
from conix.catalog import prepare_catalog
from conix.journal import (
    Block,
    HistoryConflict,
    RawEvent,
    StorageUnavailable,
    WriterBusy,
    open_journal,
)


@unittest.skipUnless(
    os.getenv("CONIX_TEST_DSN") and os.getenv("CONIX_TEST_READ_DSN"),
    "Requires the disposable installed PostgreSQL project",
)
class JournalContracts(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.catalog = prepare_catalog(Path("/tests/fixtures/catalog.toml"))

    async def test_exclusive_writer_session(self):
        runtime_dsn = os.environ["CONIX_TEST_DSN"]

        async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn):
            with self.assertRaises(WriterBusy):
                async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn):
                    self.fail("A second writer acquired the same journal.")

    async def test_complete_block_transaction_restart_and_suffix_repair(self):
        runtime_dsn = os.environ["CONIX_TEST_DSN"]
        block1 = Block(
            number=self.catalog.chain.start_block,
            hash=b"\x11" * 32,
            parent_hash=b"\x00" * 32,
            timestamp=1700000000,
        )
        block2 = Block(
            number=block1.number + 1,
            hash=b"\x22" * 32,
            parent_hash=block1.hash,
            timestamp=1700000001,
        )
        event = RawEvent(
            transaction_hash=b"\xdd" * 32,
            transaction_index=2,
            log_index=7,
            address=self.catalog.contract("token").address,
            topics=ERC20.topics,
            data=ERC20.data,
        )

        async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn) as writer:
            original = await writer.inspect()
            self.assertIsNone(original.last_block)

            published = await writer.publish_block(block=block1, events=(event,))
            self.assertEqual(published.last_block, block1)
            self.assertEqual(published.revision, original.revision + 1)
            self.assertEqual(published.generation, original.generation)

            # The database rejects a duplicated block-global occurrence.
            # The entire attempted second block, including its header,
            # must be rolled back before any progress can advance.
            with self.assertRaises(StorageUnavailable):
                await writer.publish_block(block=block2, events=(event, event))
            self.assertEqual((await writer.inspect()).last_block, block1)

            empty = await writer.publish_block(block=block2, events=())
            self.assertEqual(empty.last_block, block2)
            self.assertEqual(empty.revision, original.revision + 2)

            with self.assertRaises(HistoryConflict):
                await writer.repair_to(
                    ancestor=Block(
                        number=block1.number,
                        hash=b"\xff" * 32,
                        parent_hash=block1.parent_hash,
                        timestamp=block1.timestamp,
                    ),
                )
            self.assertEqual((await writer.inspect()).last_block, block2)

            repaired = await writer.repair_to(ancestor=block1)
            self.assertEqual(repaired.last_block, block1)
            self.assertNotEqual(repaired.generation, original.generation)
            self.assertEqual(repaired.revision, original.revision + 3)

        async with await AsyncConnection.connect(
            os.environ["CONIX_TEST_READ_DSN"],
            connect_timeout=5,
        ) as reader:
            rows = await (
                await reader.execute(
                    "SELECT b.number, e.log_index, e.topic0, e.topic1, e.data "
                    "FROM conix.blocks b LEFT JOIN conix.events e "
                    "ON e.block_number = b.number ORDER BY b.number, e.log_index"
                )
            ).fetchall()
            self.assertEqual(rows, [
                (block1.number, event.log_index, event.topics[0], event.topics[1], event.data)
            ])

        # An explicit new session obtains the checked durable basis.
        async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn) as writer:
            resumed = await writer.inspect()
            self.assertEqual(resumed.generation, repaired.generation)
            self.assertEqual(resumed.last_block, block1)

            alternate = Block(
                number=block2.number,
                hash=b"\x33" * 32,
                parent_hash=block1.hash,
                timestamp=1700000002,
            )
            finished = await writer.publish_block(block=alternate, events=())
            self.assertEqual(finished.last_block, alternate)
            self.assertEqual(finished.revision, repaired.revision + 1)


if __name__ == "__main__":
    unittest.main()
