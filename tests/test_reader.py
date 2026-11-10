"""Checkpoint-cursor binding and indexed-filter contracts without PostgreSQL."""

from dataclasses import replace
from pathlib import Path
import unittest
from uuid import UUID

from abi_golden_vectors import FROM, TO
from conix.abi import InvalidCallerValue
from conix.catalog import prepare_catalog
from conix.journal import Block
from conix.reader import (
    Checkpoint,
    CursorInvalid,
    Reader,
    _cursor_binding,
    _decode_cursor,
    _encode_cursor,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "catalog.toml"


class ReaderCursorContracts(unittest.TestCase):

    def setUp(self):
        self.catalog = prepare_catalog(FIXTURE)
        self.contract = self.catalog.contract("token")
        self.event = self.contract.descriptor.events[0]
        self.reader = Reader(catalog=self.catalog, connection=None, rpc=None)
        self.checkpoint = Checkpoint(
            source_id=self.catalog.source_id,
            generation=UUID("00000000-0000-0000-0000-000000000001"),
            history_start=1,
            block=Block(3, b"\x11" * 32, b"\x00" * 32, 1700000000),
            revision=2,
        )

    def test_filters_are_compiled_not_interpreted_as_sql(self):
        first = self.reader._filters(self.event, {"from": FROM, "to": TO})
        reordered = self.reader._filters(self.event, {"to": TO, "from": FROM})
        self.assertEqual(first, reordered)
        self.assertEqual(tuple(ordinal for (ordinal, _, _) in first), (0, 1))
        self.assertEqual(tuple(index for (_, index, _) in first), (1, 2))

        with self.assertRaises(InvalidCallerValue):
            self.reader._filters(self.event, {"value": 7})
        with self.assertRaises(InvalidCallerValue):
            self.reader._filters(self.event, {"from": 7})

    def test_cursor_binds_exact_checkpoint_event_and_canonical_filters(self):
        filters = self.reader._filters(self.event, {"to": TO})
        binding = _cursor_binding(self.checkpoint, self.contract, self.event, filters)
        position = (2, 0, 9)
        cursor = _encode_cursor(binding, position)

        self.assertEqual(_decode_cursor(cursor, binding, self.checkpoint), position)
        self.assertEqual(len(cursor), len(cursor.encode("ascii")))

        different_generation = replace(
            self.checkpoint,
            generation=UUID("00000000-0000-0000-0000-000000000002"),
        )
        with self.assertRaises(CursorInvalid):
            _decode_cursor(cursor, _cursor_binding(
                different_generation, self.contract, self.event, filters
            ), different_generation)

        different_filter = self.reader._filters(self.event, {"from": FROM})
        with self.assertRaises(CursorInvalid):
            _decode_cursor(cursor, _cursor_binding(
                self.checkpoint, self.contract, self.event, different_filter
            ), self.checkpoint)

        with self.assertRaises(CursorInvalid):
            _decode_cursor(cursor + "!", binding, self.checkpoint)
        with self.assertRaises(CursorInvalid):
            _decode_cursor("a" * 600, binding, self.checkpoint)

    def test_cursor_cannot_jump_beyond_its_selected_checkpoint(self):
        binding = _cursor_binding(self.checkpoint, self.contract, self.event, ())
        after = _encode_cursor(binding, (4, 0, 0))

        with self.assertRaises(CursorInvalid):
            _decode_cursor(after, binding, self.checkpoint)


if __name__ == "__main__":
    unittest.main()
