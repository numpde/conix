"""Explicit recovery uses stored identities and the core checked repair."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from conix.history_cli import inspect_history, repair_history
from conix.journal import Block, HistoryConflict


BLOCK = Block(7, b"a" * 32, b"b" * 32, 123)


class HistoryOperatorContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = SimpleNamespace(
            source_id=b"s" * 32,
            chain=SimpleNamespace(rpc_url="http://selected.invalid"),
        )

    async def test_inspect_does_not_require_rpc_or_canonical_history(self):
        basis = SimpleNamespace(
            generation=uuid4(), revision=9, blocked_reason="fork",
            last_block=BLOCK,
        )
        journal = SimpleNamespace(
            basis=basis,
            retained_blocks=self._retained_blocks,
        )

        @asynccontextmanager
        async def opened(**_kwargs):
            yield journal

        with patch("conix.history_cli.open_journal", opened):
            result = await inspect_history(self.catalog, "secret-dsn")
        self.assertEqual(result["checkpoint"], 7)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["blocks"][0]["hash"], "0x" + "61" * 32)
        self.assertNotIn("fork", str(result))

    async def _retained_blocks(self, *, limit):
        self.assertEqual(limit, 65)
        return (BLOCK,)

    async def test_exact_repair_uses_original_stored_identity(self):
        calls = []
        basis = SimpleNamespace(generation=uuid4(), revision=10, last_block=BLOCK)
        class Writer:
            journal = SimpleNamespace(retained_block=lambda _number: None)

            async def repair_to(self, *, ancestor):
                calls.append(ancestor)
                return basis

        writer = Writer()

        async def retained(number):
            self.assertEqual(number, 7)
            return BLOCK

        writer.journal.retained_block = retained

        @asynccontextmanager
        async def opened(**_kwargs):
            yield writer

        with patch("conix.history_cli.open_writer", opened):
            result = await repair_history(self.catalog, "secret-dsn", ancestor=7)
        self.assertEqual(calls, [BLOCK])
        self.assertEqual(result["outcome"], "repaired")
        self.assertEqual(result["ancestor"]["number"], 7)

    async def test_missing_ancestor_refuses_repair(self):
        class Writer:
            async def repair_to(self, **_kwargs):
                self.fail("No fabricated ancestor should reach repair.")

        async def missing(_number):
            return None

        writer = Writer()
        writer.journal = SimpleNamespace(retained_block=missing)
        @asynccontextmanager
        async def opened(**_kwargs):
            yield writer

        with patch("conix.history_cli.open_writer", opened):
            with self.assertRaises(HistoryConflict):
                await repair_history(self.catalog, "secret-dsn", ancestor=6)
