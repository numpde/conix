"""Explicit operator recovery delegates to the proven retained-ancestry API."""

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
            limits=SimpleNamespace(max_evidence_blocks=64),
        )

    async def test_inspect_does_not_require_rpc_or_canonical_history(self):
        basis = SimpleNamespace(
            source_id=b"s" * 32, history_start=7,
            generation=uuid4(), revision=9, blocked_reason="fork",
            last_block=BLOCK,
        )
        ancestry = SimpleNamespace(progress=basis, blocks=(BLOCK,))

        async def inspected(**kwargs):
            self.assertEqual(kwargs["limit"], 64)
            self.assertIsNone(kwargs["through"])
            return ancestry

        with patch("conix.history_cli.inspect_retained_ancestry", inspected):
            result = await inspect_history(self.catalog, "secret-dsn")
        self.assertEqual(result["checkpoint"], 7)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["blocks"][0]["hash"], "0x" + "61" * 32)
        self.assertNotIn("fork", str(result))
        self.assertFalse(result["more_older_blocks"])

    async def test_exact_repair_uses_original_stored_identity(self):
        calls = []
        basis = SimpleNamespace(generation=uuid4(), revision=10, last_block=BLOCK)

        async def retained(*, limit, through):
            self.assertEqual((limit, through), (1, 7))
            return SimpleNamespace(blocks=(BLOCK,))

        class Writer:
            journal = SimpleNamespace(retained_ancestry=retained)

            async def repair_to(self, *, ancestor):
                calls.append(ancestor)
                return basis

        @asynccontextmanager
        async def opened(**_kwargs):
            yield Writer()

        with patch("conix.history_cli.open_writer", opened):
            result = await repair_history(self.catalog, "secret-dsn", ancestor=7)
        self.assertEqual(calls, [BLOCK])
        self.assertEqual(result["outcome"], "repaired")
        self.assertEqual(result["ancestor"]["number"], 7)

    async def test_missing_ancestor_refuses_repair(self):
        async def missing(*, limit, through):
            raise HistoryConflict("No retained ancestor at this height.")

        class Writer:
            journal = SimpleNamespace(retained_ancestry=missing)

            async def repair_to(self, **_kwargs):
                self.fail("Missing retained ancestor must never invoke repair.")

        @asynccontextmanager
        async def opened(**_kwargs):
            yield Writer()

        with patch("conix.history_cli.open_writer", opened):
            with self.assertRaises(HistoryConflict):
                await repair_history(self.catalog, "secret-dsn", ancestor=6)
