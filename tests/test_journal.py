"""Exercise the installed one-block PostgreSQL publisher and checked repair."""

import asyncio
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import signal
import unittest
from unittest.mock import patch
from uuid import UUID

import psycopg
from psycopg import AsyncConnection

from abi_golden_vectors import ERC20
from pg_commit_reply import without_commit_reply
from conix.catalog import prepare_catalog
from conix import journal
from conix.journal import (
    Block,
    HistoryConflict,
    CommitOutcomeUnknown,
    Journal,
    Progress,
    RawEvent,
    StorageUnavailable,
    WriterBusy,
    open_journal,
)


async def _pause_before_commit(writer, block, event, ready):
    advance_progress = writer._advance_progress

    async def after_progress(**kwargs):
        await advance_progress(**kwargs)
        ready()
        await asyncio.Future()

    with patch.object(writer, "_advance_progress", after_progress):
        await writer.publish_block(block=block, events=(event,))


def _publish_in_child(block, event, readiness):
    async def publish():
        catalog = prepare_catalog(Path("/tests/fixtures/catalog.toml"))
        async with open_journal(catalog=catalog, database_dsn=os.environ["CONIX_TEST_DSN"]) as writer:
            await _pause_before_commit(writer, block, event, lambda: readiness.send_bytes(b"transaction ready"))

    try:
        asyncio.run(publish())
    except Exception as error:
        readiness.send_bytes(f"Child publication failed ({type(error).__name__}).".encode("ascii"))
        raise SystemExit(1) from None
    finally:
        readiness.close()


class UncertainCommitContracts(unittest.IsolatedAsyncioTestCase):
    """A lost commit reply retires its writer instead of replaying a block."""

    async def test_unknown_commit_retires_the_connection(self):
        class LostReply:
            closed = False

            async def execute(self, statement, _parameters=None):
                if statement == "COMMIT":
                    raise psycopg.OperationalError("commit reply was lost")
                return self

            async def close(self):
                self.closed = True

        catalog = prepare_catalog(Path(__file__).resolve().parent / "fixtures" / "catalog.toml")
        connection = LostReply()
        writer = Journal(
            connection=connection,
            catalog=catalog,
            basis=Progress(
                source_id=catalog.source_id,
                generation=UUID("00000000-0000-0000-0000-000000000001"),
                history_start=catalog.chain.start_block,
                revision=0,
                last_block=None,
                blocked_reason=None,
            ),
        )
        with self.assertRaises(CommitOutcomeUnknown):
            async with writer._transaction():
                pass

        self.assertTrue(writer.retired)
        self.assertTrue(connection.closed)
        with self.assertRaises(HistoryConflict):
            await writer.inspect()


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

            # Another authorized session can block a source without following
            # the advisory-lock protocol. The publication UPDATE must reject
            # that state using its own atomic condition.
            async with await AsyncConnection.connect(runtime_dsn, connect_timeout=5) as other:
                await other.execute(
                    "UPDATE conix.source_progress SET blocked_reason = %s WHERE singleton = 1",
                    ("requires repair",),
                )
            try:
                with self.assertRaises(HistoryConflict):
                    await writer.publish_block(block=block1, events=(event,))
                self.assertIsNone((await writer.inspect()).last_block)
            finally:
                async with await AsyncConnection.connect(runtime_dsn, connect_timeout=5) as other:
                    await other.execute(
                        "UPDATE conix.source_progress SET blocked_reason = NULL WHERE singleton = 1"
                    )

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

            # The hash identifies the ancestor; do not turn its auxiliary
            # timestamp field into a second, independently trusted identity.
            identity = Block(
                number=block1.number,
                hash=block1.hash,
                parent_hash=block1.parent_hash,
                timestamp=block1.timestamp + 1,
            )
            repaired = await writer.repair_to(ancestor=identity)
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

        # Once explicitly blocked, the journal must remain accessible for
        # inspection and a checked manual repair, not for further appends.
        async with await AsyncConnection.connect(runtime_dsn, connect_timeout=5) as other:
            await other.execute(
                "UPDATE conix.source_progress SET blocked_reason = %s WHERE singleton = 1",
                ("reorganization requires repair",),
            )

        async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn) as writer:
            observed = await writer.inspect()
            self.assertEqual(observed.blocked_reason, "reorganization requires repair")

            block3 = Block(
                number=alternate.number + 1,
                hash=b"\x44" * 32,
                parent_hash=alternate.hash,
                timestamp=1700000003,
            )
            with self.assertRaises(HistoryConflict):
                await writer.publish_block(block=block3, events=())

            recovered = await writer.repair_to(ancestor=block1)
            self.assertEqual(recovered.last_block, block1)
            self.assertIsNone(recovered.blocked_reason)
            self.assertNotEqual(recovered.generation, finished.generation)

        attempted = replace(event, transaction_hash=b"\xee" * 32)
        await self.check_publication_cancellation(recovered, alternate, attempted)
        continued = await self.check_publication_process_loss(recovered, alternate, attempted)
        fenced = await self.check_repair_under_contention(continued, block1, attempted)
        next_block = Block(
            number=alternate.number + 1, hash=b"\x55" * 32,
            parent_hash=alternate.hash, timestamp=alternate.timestamp + 1,
        )
        await self.check_unconfirmed_publication(fenced, next_block, replace(event, transaction_hash=b"\xff" * 32))

    async def check_repair_under_contention(self, before, ancestor, retained_event):
        """A changed durable basis must roll back the repair's suffix deletion."""
        runtime_dsn = os.environ["CONIX_TEST_DSN"]
        async with asyncio.timeout(10), open_journal(catalog=self.catalog, database_dsn=runtime_dsn) as writer:
            async def reject_repair():
                with self.assertRaises(HistoryConflict):
                    await writer.repair_to(ancestor=ancestor)

            async with await AsyncConnection.connect(runtime_dsn, connect_timeout=5) as blocker:
                await blocker.execute("UPDATE conix.source_progress SET revision = revision + 1 WHERE singleton = 1")
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(reject_repair())
                    while True:
                        row = await (await blocker.execute(
                            "SELECT pg_blocking_pids(%s)", (writer.connection.info.backend_pid,),
                        )).fetchone()
                        if blocker.info.backend_pid in row[0]:
                            break
                        await asyncio.sleep(0.05)

                    retained = [self.expected_row(before.last_block, retained_event)]
                    self.assertEqual(await self.candidate_rows(before.last_block), retained)
                    with self.assertRaises(WriterBusy):
                        async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn):
                            self.fail("A second writer acquired the journal during repair.")
                    await blocker.commit()

            expected = replace(before, revision=before.revision + 1)
            self.assertEqual(await writer.inspect(), expected)
            self.assertEqual(await self.candidate_rows(before.last_block), retained)
            return expected

    async def check_publication_cancellation(self, before, block, event):
        """Cancel after real DML and verify the original session can still inspect."""
        async with asyncio.timeout(10), open_journal(
            catalog=self.catalog, database_dsn=os.environ["CONIX_TEST_DSN"],
        ) as writer:
            ready = asyncio.Event()
            async with asyncio.TaskGroup() as tasks:
                pending = tasks.create_task(_pause_before_commit(writer, block, event, ready.set))
                await ready.wait()
                self.assertEqual(await self.candidate_rows(block), [])
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                self.assertEqual(await writer.inspect(), before)
                self.assertEqual(await self.candidate_rows(block), [])

    async def check_publication_process_loss(self, before, block, event):
        """Kill only the child owning the uncommitted writer transaction."""
        context = multiprocessing.get_context("spawn")
        (readiness, sender) = context.Pipe(duplex=False)
        with readiness, sender:
            child = context.Process(target=_publish_in_child, args=(block, event, sender))
            child.start()
            self.addCleanup(self.retire_child, child)
            sender.close()

            self.assertTrue(await asyncio.to_thread(readiness.poll, 10), "Writer child never reached the commit boundary.")
            self.assertEqual(readiness.recv_bytes(128), b"transaction ready")
            self.assertEqual(await self.candidate_rows(block), [])
            child.kill()
            await asyncio.to_thread(child.join, 10)
            self.assertEqual(child.exitcode, -signal.SIGKILL)

        writer = await self.reopen_after_disconnect()
        try:
            self.assertEqual(await writer.inspect(), before)
            self.assertEqual(await self.candidate_rows(block), [])
            published = await writer.publish_block(block=block, events=(event,))
            self.assertEqual(published, replace(before, last_block=block, revision=before.revision + 1))
            self.assertEqual(await self.candidate_rows(block), [self.expected_row(block, event)])
        finally:
            await writer.close()
        return published

    def retire_child(self, child):
        if child.is_alive():
            child.kill()
            child.join(10)
        self.assertFalse(child.is_alive(), "Writer child retirement was not confirmed.")
        child.close()

    async def reopen_after_disconnect(self):
        # PostgreSQL can observe a closed socket after local retirement.
        async with asyncio.timeout(10):
            while True:
                try:
                    return await Journal.open(catalog=self.catalog, database_dsn=os.environ["CONIX_TEST_DSN"])
                except WriterBusy:
                    await asyncio.sleep(0.05)

    async def candidate_rows(self, block):
        async with asyncio.timeout(5), await AsyncConnection.connect(
            os.environ["CONIX_TEST_READ_DSN"], connect_timeout=5,
        ) as reader:
            return await (
                await reader.execute(
                    "SELECT b.hash, e.transaction_hash, e.transaction_index, "
                    "e.log_index, e.address, e.topic0, e.topic1, e.topic2, e.topic3, e.data "
                    "FROM conix.blocks b LEFT JOIN conix.events e ON e.block_number = b.number "
                    "WHERE b.number = %s",
                    (block.number,),
                )
            ).fetchall()

    def expected_row(self, block, event):
        return (
            block.hash, event.transaction_hash, event.transaction_index,
            event.log_index, event.address, *event.topics, None, event.data,
        )

    async def check_unconfirmed_publication(self, before, block, event):
        """Compare an unsent commit with a real lost completion response."""
        runtime_dsn = os.environ["CONIX_TEST_DSN"]
        async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn) as writer:
            execute = writer.connection.execute

            async def lose_before_send(statement, *args):
                if statement == "COMMIT":
                    raise psycopg.OperationalError("injected loss before COMMIT was sent")
                return await execute(statement, *args)

            with patch.object(writer.connection, "execute", lose_before_send):
                attempt = (await self.expect_unknown_commit(writer, block, event)).attempt
            self.assertEqual(attempt, journal.PublicationAttempt(before, block, (event,)))

        async with open_journal(catalog=self.catalog, database_dsn=runtime_dsn) as reopened:
            self.assertEqual(await reopened.inspect(), before)
            self.assertEqual(
                await reopened.reconcile_publication(attempt),
                journal.PublicationObservation.ORIGINAL_BASIS_PRESERVED,
            )
            self.assertEqual(await reopened.inspect(), before)
        self.assertEqual(await self.candidate_rows(block), [])

        async with asyncio.timeout(10), without_commit_reply(runtime_dsn) as (relay_dsn, dropped):
            async with open_journal(catalog=self.catalog, database_dsn=relay_dsn) as writer:
                attempt = (await self.expect_unknown_commit(writer, block, event)).attempt
            self.assertEqual(attempt, journal.PublicationAttempt(before, block, (event,)))
            await dropped

        reopened = await self.reopen_after_disconnect()
        try:
            expected = replace(before, last_block=block, revision=before.revision + 1)
            self.assertEqual(await reopened.inspect(), expected)
            self.assertEqual(
                await reopened.reconcile_publication(attempt),
                journal.PublicationObservation.EXPECTED_EFFECT_PRESENT,
            )
            self.assertEqual(await reopened.inspect(), expected)
            with self.assertRaises(HistoryConflict):
                await reopened.publish_block(block=block, events=(event,))
            self.assertEqual(await reopened.inspect(), expected)
            self.assertEqual(await self.candidate_rows(block), [self.expected_row(block, event)])
        finally:
            await reopened.close()

    async def expect_unknown_commit(self, writer, block, event):
        with self.assertRaises(CommitOutcomeUnknown) as failure:
            await writer.publish_block(block=block, events=(event,))
        self.assertIsInstance(failure.exception.__cause__, psycopg.OperationalError)
        self.assertTrue(writer.retired)
        self.assertTrue(writer.connection.closed)
        return failure.exception


if __name__ == "__main__":
    unittest.main()
