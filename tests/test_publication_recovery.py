"""The original journal observes exact publication effects without replay."""

import asyncio
from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg

from conix import journal
from conix.catalog import prepare_catalog


@unittest.skipUnless(os.getenv("CONIX_TEST_DSN"), "Requires disposable PostgreSQL")
class PublicationObservationContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.catalog = prepare_catalog(Path("/tests/fixtures/catalog.toml"))
        self.dsn = os.environ["CONIX_TEST_DSN"]

    async def candidate(self, events=()):
        async with journal.open_journal(catalog=self.catalog, database_dsn=self.dsn) as writer:
            basis = await writer.inspect()
        previous = basis.last_block
        block = journal.Block(
            previous.number + 1 if previous else basis.history_start,
            b"\x91" * 32, previous.hash if previous else b"\x00" * 32, 1700000000,
        )
        return journal.PublicationAttempt(basis, block, events)

    async def observe(self, attempt):
        async with journal.open_journal(catalog=self.catalog, database_dsn=self.dsn) as writer:
            before = await writer.inspect()
            result = await writer.reconcile_publication(attempt)
            self.assertEqual(await writer.inspect(), before, "Inspection changed durable progress.")
            return result

    async def test_exact_empty_and_multiple_event_effects_and_mismatches(self):
        """Every stored field and the entire event collection determine a match."""
        event = journal.RawEvent(b"\x81" * 32, 1, 2, b"\x11" * 20, (b"\x82" * 32,), b"payload")
        for events in ((), (replace(event, log_index=5), event)):
            attempt = await self.candidate(events)
            with self.assertRaises(FrozenInstanceError):
                attempt.events = ()
            self.assertIs(await self.observe(attempt), journal.PublicationObservation.ORIGINAL_BASIS_PRESERVED)
            async with journal.open_journal(catalog=self.catalog, database_dsn=self.dsn) as writer:
                await writer.publish_block(block=attempt.block, events=attempt.events)
            self.assertIs(await self.observe(attempt), journal.PublicationObservation.EXPECTED_EFFECT_PRESENT)

            candidates = [
                replace(attempt, block=replace(attempt.block, hash=b"\x99" * 32)),
                replace(attempt, block=replace(attempt.block, parent_hash=b"\x98" * 32)),
                replace(attempt, block=replace(attempt.block, timestamp=42)),
                replace(attempt, basis=replace(attempt.basis, revision=attempt.basis.revision + 1)),
                replace(attempt, basis=replace(attempt.basis, generation=uuid4())),
                replace(attempt, basis=replace(attempt.basis, blocked_reason="operator hold")),
                replace(attempt, events=events + (replace(event, log_index=99),)),
            ]
            if events:
                candidates.append(replace(attempt, events=events[:1]))
                for field, value in {
                    "transaction_hash": b"\x99" * 32, "transaction_index": 9,
                    "log_index": 19, "address": b"\x55" * 20,
                    "topics": (b"\x55" * 32,), "data": b"different",
                }.items():
                    candidates.append(replace(attempt, events=(events[0], replace(events[1], **{field: value}))))
            for candidate in candidates:
                with self.subTest(candidate=candidates.index(candidate)):
                    self.assertIs(await self.observe(candidate), journal.PublicationObservation.OTHER_DURABLE_STATE)

            wrong_source = replace(attempt, basis=replace(attempt.basis, source_id=b"\x99" * 32))
            with self.assertRaises(journal.HistoryConflict):
                await self.observe(wrong_source)

    async def test_later_publication_is_another_state_and_retired_writer_refuses(self):
        """A later valid advance is unresolved by the strict comparison, not corruption."""
        attempt = await self.candidate()
        async with journal.open_journal(catalog=self.catalog, database_dsn=self.dsn) as writer:
            await writer.publish_block(block=attempt.block, events=())
            following = replace(attempt.block, number=attempt.block.number + 1, parent_hash=attempt.block.hash)
            await writer.publish_block(block=following, events=())
        self.assertIs(await self.observe(attempt), journal.PublicationObservation.OTHER_DURABLE_STATE)
        with self.assertRaises(journal.HistoryConflict):
            await writer.reconcile_publication(attempt)

    async def test_observation_storage_failure_and_cancellation_do_not_become_results(self):
        """Failed or interrupted reads supply no recovery permission."""
        attempt = await self.candidate()
        for failure in (psycopg.OperationalError("private-fixture-text"), asyncio.CancelledError()):
            async with journal.open_journal(catalog=self.catalog, database_dsn=self.dsn) as writer:
                execute = writer.connection.execute

                async def fail_read(statement, *args):
                    if statement.startswith("SELECT number, hash"):
                        raise failure
                    return await execute(statement, *args)

                with patch.object(writer.connection, "execute", fail_read):
                    expected = journal.StorageUnavailable if isinstance(failure, Exception) else asyncio.CancelledError
                    with self.assertRaises(expected) as caught:
                        await writer.reconcile_publication(attempt)
                if isinstance(failure, Exception):
                    self.assertIs(caught.exception.__cause__, failure)
                    self.assertNotIn("private-fixture-text", str(caught.exception))
                self.assertEqual(await writer.inspect(), attempt.basis)

    async def test_uncertain_repair_never_carries_publication_context(self):
        """The shared transaction helper must not turn repair into publication."""
        attempt = await self.candidate()
        async with journal.open_journal(catalog=self.catalog, database_dsn=self.dsn) as writer:
            await writer.publish_block(block=attempt.block, events=())
            execute = writer.connection.execute

            async def unsent(statement, *args):
                if statement == "COMMIT":
                    raise psycopg.OperationalError("fixture COMMIT not sent")
                return await execute(statement, *args)

            with patch.object(writer.connection, "execute", unsent):
                with self.assertRaises(journal.CommitOutcomeUnknown) as caught:
                    await writer.repair_to(ancestor=attempt.block)
            self.assertNotIsInstance(caught.exception, journal.PublicationOutcomeUnknown)
            self.assertTrue(writer.connection.closed)
