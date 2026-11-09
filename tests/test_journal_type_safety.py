"""Exact collection-field persistence contracts against REAL PostgreSQL.

The existing Tier-0 store is PayToPlay-specific. These tests establish a
regression baseline for its ABI-backed widths and bytes; they do NOT qualify
the proposed generic events table or its public filter API.
"""
import os
import unittest
from dataclasses import replace

import psycopg

from conix.engine import advance_one
from conix.journal import Journal
from conix.models import CollectionCursor, HistoryUnavailable
from conix.observer import Observer

from test_tier0 import Chain, block, event, settings


@unittest.skipUnless(os.getenv("CONIX_TEST_DSN"), "Requires isolated PostgreSQL with the enrolled Conix test source")
class PostgreSQLTypeSafetyContracts(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.settings = settings()
        self.journal = Journal(os.environ["CONIX_TEST_DSN"], self.settings)

        # Reset via the checked public repair path, not administrative SQL.
        first = block(1, 1, bytes(32))
        source = Chain(self.settings, (first,))

        async with self.journal.writer() as writer:
            await writer.repair_to(
                expected_basis=(await writer.basis()),
                repair_target=None,
                chain=source,
            )

    async def _index(self, first, records, second=None):
        selected_blocks = (first,) if second is None else (first, second)
        source = Chain(self.settings, selected_blocks, records)

        async with self.journal.writer() as writer:
            for _ in selected_blocks:
                await advance_one(writer, source)

        return source

    async def test_uint256_extremes_and_raw_bytes_survive_sql_and_reader(self):
        first = block(1, 21, bytes(32))
        second = block(2, 22, first.hash)
        max_uint = (1 << 256) - 1

        source_events = (
            replace(
                event(first, index=0, amount=1),
                channel_id=max_uint,
                voucher_paid=max_uint,
                invoice_hash=bytes(32),
            ),
            replace(
                event(first, index=1, amount=1 << 255),
                channel_id=1 << 255,
                voucher_paid=max_uint,
                invoice_hash=b"\xff" * 32,
            ),
            replace(
                event(first, index=2, amount=max_uint),
                channel_id=1 << 64,
                voucher_paid=max_uint,
                invoice_hash=b"\x00" * 31 + b"\xff",
            ),
        )

        source = await self._index(first, {first.number: source_events}, second)
        observer = Observer(self.journal, source)
        checkpoint = await observer.latest_checkpoint()

        collected = []
        cursor = None
        for _ in range(len(source_events) + 1):
            page = await observer.voucher_collected_events(
                checkpoint,
                cursor=cursor,
                limit=1,
            )
            collected.extend(page.voucher_collected_events)
            cursor = page.cursor

            if page.complete:
                break
        else:
            self.fail("Collection pages did not cover the completed prefix.")

        self.assertEqual(tuple(collected), source_events)
        self.assertEqual(checkpoint.collection_count, len(source_events))
        self.assertEqual((checkpoint.block.number, page.complete), (2, True))

        # Inspect the actual SQL textual representation and bytes, not only
        # values supplied to the journal's INSERT statement.
        async with self.journal._connection() as connection:
            cursor = await connection.execute(
                "SELECT sequence, channel_id::text, amount::text, "
                "voucher_paid::text, encode(invoice_hash, 'hex') AS invoice "
                "FROM conix.collections ORDER BY sequence"
            )
            stored_rows = await cursor.fetchall()

        for expected_sequence, (original, row) in enumerate(zip(source_events, stored_rows, strict=True)):
            with self.subTest(sequence=expected_sequence):
                self.assertEqual(int(row["sequence"]), expected_sequence)
                self.assertEqual(row["channel_id"], str(original.channel_id))
                self.assertEqual(row["amount"], str(original.amount))
                self.assertEqual(row["voucher_paid"], str(original.voucher_paid))
                self.assertEqual(row["invoice"], original.invoice_hash.hex())

        # New Reader and Journal instances must see exactly the same values,
        # never a float/JSON serialization or stale Python object.
        fresh = Observer(Journal(os.environ["CONIX_TEST_DSN"], self.settings), source)
        new_checkpoint = await fresh.latest_checkpoint()
        self.assertEqual(new_checkpoint.generation, checkpoint.generation)
        fresh_page = await fresh.voucher_collected_events(new_checkpoint, cursor=None, limit=10)
        self.assertEqual(fresh_page.voucher_collected_events, source_events)

    async def test_sql_numeric_equality_preserves_full_uint256(self):
        first = block(1, 31, bytes(32))
        max_uint = (1 << 256) - 1
        source_events = (
            replace(event(first, index=0, amount=1), channel_id=max_uint, voucher_paid=max_uint),
            replace(event(first, index=1, amount=1), channel_id=max_uint - 1, voucher_paid=max_uint),
            replace(event(first, index=2, amount=1), channel_id=1 << 255, voucher_paid=max_uint),
        )
        await self._index(first, {1: source_events})

        # This tests PostgreSQL's exact numeric binding, not yet Conix's
        # proposed generic, ABI-validated public equality filter.
        async with self.journal._connection() as connection:
            for target, expected in (
                (max_uint, (0,)),
                (max_uint - 1, (1,)),
                (1 << 255, (2,)),
                (1 << 254, ()),
            ):
                with self.subTest(target=target):
                    rows = await (await connection.execute(
                        "SELECT sequence FROM conix.collections "
                        "WHERE channel_id = %s ORDER BY sequence",
                        (target,),
                    )).fetchall()
                    self.assertEqual(tuple(int(row["sequence"]) for row in rows), expected)

    async def test_wrong_length_fixed_bytes_fails_without_partial_publication(self):
        first = block(1, 41, bytes(32))
        invalid = replace(event(first, amount=7), invoice_hash=bytes(31))
        source = Chain(self.settings, (first,), {1: (invalid,)})

        async with self.journal.writer() as writer:
            with self.assertRaises(psycopg.errors.CheckViolation):
                await advance_one(writer, source)

            unchanged = await writer.basis()
            self.assertIsNone(unchanged.checkpoint)
            self.assertEqual(unchanged.collection_count, 0)

            source.contract.records[1] = (replace(invalid, invoice_hash=bytes(32)),)
            result = await advance_one(writer, source)
            self.assertTrue(result.changed)

        self.assertEqual((await self.journal.basis()).collection_count, 1)

    async def test_reorg_replaces_typed_values_and_invalidates_old_cursors(self):
        first = block(1, 51, bytes(32))
        original_second = block(2, 52, first.hash)
        replacement_second = block(2, 53, first.hash)

        max_uint = (1 << 256) - 1
        first_event = replace(event(first), channel_id=max_uint, voucher_paid=max_uint)
        orphaned = replace(
            event(original_second),
            invoice_hash=b"\xff" * 32,
            amount=1 << 255,
            voucher_paid=max_uint,
        )
        replacement = replace(
            event(replacement_second),
            invoice_hash=b"\x00" * 32,
            amount=max_uint,
            voucher_paid=max_uint,
        )
        source = await self._index(first, {1: (first_event,), 2: (orphaned,)}, original_second)
        observer = Observer(self.journal, source)
        checkpoint = await observer.latest_checkpoint()
        old_page = await observer.voucher_collected_events(checkpoint, cursor=None, limit=1)

        async with self.journal.writer() as writer:
            await writer.repair_to(
                expected_basis=(await writer.basis()),
                repair_target=first,
                chain=source,
            )
            source.blocks[2] = replacement_second
            source.contract.records[2] = (replacement,)
            await advance_one(writer, source)

        new_checkpoint = await observer.latest_checkpoint()
        self.assertNotEqual(new_checkpoint.generation, checkpoint.generation)

        with self.assertRaises(HistoryUnavailable):
            await observer.voucher_collected_events(
                checkpoint,
                cursor=old_page.cursor,
                limit=1,
            )

        page = await observer.voucher_collected_events(new_checkpoint, cursor=None, limit=10)
        self.assertEqual(page.voucher_collected_events, (first_event, replacement))
        self.assertEqual(page.voucher_collected_events[-1].amount, max_uint)

        async with self.journal._connection() as connection:
            rows = await (await connection.execute(
                "SELECT encode(invoice_hash, 'hex') AS invoice "
                "FROM conix.collections ORDER BY sequence"
            )).fetchall()
        self.assertEqual(
            tuple(row["invoice"] for row in rows),
            (first_event.invoice_hash.hex(), replacement.invoice_hash.hex()),
        )


if __name__ == "__main__":
    unittest.main()
