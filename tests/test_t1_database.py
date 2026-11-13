"""T1-D1 behavioral recovery of the original application after PostgreSQL disconnection.

The first test-first run uses a pass-through composition: a real PostgreSQL
backend is terminated while the original indexer is acquiring selected RPC
evidence. The baseline supervisor must still fail; the green implementation
must retire that writer and resume through the original application and journal.
"""

import asyncio
from contextlib import asynccontextmanager
import os
import unittest
from unittest.mock import patch

import httpx
import psycopg
from psycopg import AsyncConnection

from anvil_catalog import catalog_for_anvil
from conix import app
from conix.core import BranchConflict
from conix.journal import (
    CommitOutcomeUnknown, HistoryConflict, Journal, StorageUnavailable, WriterBusy,
)
from conix.resilience import database, reorg, rpc_retry, supervisor
from conix.rpc import RPC, RpcUnavailable


def storage_error(cause):
    failure = StorageUnavailable("PostgreSQL operation could not complete.")
    failure.__cause__ = cause
    return failure


class DatabaseSessionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_retry_new_application_only_after_confirmed_retirement(self):
        """This capability is absent from the unchanged existing supervisor."""
        source = object()
        dsn = object()
        stop = asyncio.Event()
        previous = storage_error(psycopg.OperationalError("connection broken"))
        entered = 0
        retired = 0

        async def application(catalog, database_dsn, stopped):
            nonlocal entered, retired
            self.assertIs(catalog, source)
            self.assertIs(database_dsn, dsn)
            self.assertIs(stopped, stop)
            self.assertEqual(entered, retired, "A new session began before retirement.")
            entered += 1
            try:
                if entered == 1:
                    raise previous
                stop.set()
            finally:
                retired += 1

        with patch.object(database, "supervise_with_reorg_recovery", application):
            await database.supervise_with_database_recovery(source, dsn, stop)

        self.assertEqual((entered, retired), (2, 2))

    async def test_permanent_sql_source_and_unknown_commit_failures_do_not_restart(self):
        failures = [
            storage_error(psycopg.errors.InvalidPassword("wrong password")),
            storage_error(psycopg.errors.UndefinedTable("schema missing")),
            storage_error(psycopg.errors.UniqueViolation("SQL constraint failed")),
            StorageUnavailable("Cause and transport unconfirmed."),
            CommitOutcomeUnknown("COMMIT reply lost"),
            HistoryConflict("durable source blocked"),
            WriterBusy("another writer owns journal"),
            BranchConflict("wrong blockchain"),
            RpcUnavailable("RPC failed"),
            OSError("failed retirement"),
        ]
        for failure in failures:
            with self.subTest(cause=type(failure).__name__, sqlstate=getattr(failure.__cause__, "sqlstate", None)):
                attempts = []

                async def application(*_args):
                    attempts.append(True)
                    raise failure

                with patch.object(database, "supervise_with_reorg_recovery", application):
                    with self.assertRaises(type(failure)) as caught:
                        await database.supervise_with_database_recovery(
                            object(), object(), asyncio.Event(),
                        )
                self.assertIs(caught.exception, failure)
                self.assertEqual(attempts, [True])

    async def test_only_two_fresh_sessions_per_database_failure_budget(self):
        attempts = []
        failures = [
            storage_error(psycopg.errors.ConnectionFailure("connection lost")),
            storage_error(psycopg.errors.AdminShutdown("backend terminated")),
            storage_error(psycopg.OperationalError("lost transport")),
        ]

        async def application(*_args):
            attempts.append(True)
            raise failures[len(attempts) - 1]

        with patch.object(database, "supervise_with_reorg_recovery", application):
            with self.assertRaises(StorageUnavailable) as caught:
                await database.supervise_with_database_recovery(
                    object(), object(), asyncio.Event(),
                )
        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(len(attempts), 3)

    async def test_stop_during_backoff_preserves_failure_without_another_session(self):
        stop = asyncio.Event()
        entered = asyncio.Event()
        failure = storage_error(psycopg.OperationalError("transport failed"))
        attempts = []

        async def application(*_args):
            attempts.append(True)
            entered.set()
            raise failure

        with patch.object(database, "supervise_with_reorg_recovery", application):
            task = asyncio.create_task(database.supervise_with_database_recovery(
                object(), object(), stop,
            ))
            await asyncio.wait_for(entered.wait(), timeout=2)
            stop.set()
            with self.assertRaises(StorageUnavailable) as caught:
                await asyncio.wait_for(task, timeout=3)
        self.assertIs(caught.exception, failure)
        self.assertEqual(attempts, [True])

    async def test_task_cancellation_during_retry_backoff_propagates(self):
        entered = asyncio.Event()
        attempts = []

        async def application(*_args):
            attempts.append(True)
            entered.set()
            raise storage_error(psycopg.errors.ConnectionFailure("offline"))

        with patch.object(database, "supervise_with_reorg_recovery", application):
            task = asyncio.create_task(database.supervise_with_database_recovery(
                object(), object(), asyncio.Event(),
            ))
            await asyncio.wait_for(entered.wait(), timeout=2)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0.12)
        self.assertEqual(attempts, [True])

    async def test_cleanup_failure_is_not_disconnection_authority(self):
        failure = OSError("Writer close failed.")
        attempts = []

        async def application(*_args):
            attempts.append(True)
            try:
                raise storage_error(psycopg.OperationalError("connection lost"))
            finally:
                raise failure

        with patch.object(database, "supervise_with_reorg_recovery", application):
            with self.assertRaises(OSError) as caught:
                await database.supervise_with_database_recovery(
                    object(), object(), asyncio.Event(),
                )
        self.assertIs(caught.exception, failure)
        self.assertEqual(attempts, [True])

    async def test_original_t1b_remains_fail_closed_on_sql_disconnect(self):
        """T1-D is opt-in: ordinary Tier-0/T1-B never acquire this policy."""
        previous = storage_error(psycopg.errors.ConnectionFailure("connection lost"))
        attempts = []

        async def original_app(*_args):
            attempts.append(1)
            raise previous

        with patch.object(supervisor, "run_with_read_retries", original_app):
            with self.assertRaises(StorageUnavailable) as caught:
                await supervisor.supervise(object(), object(), asyncio.Event())
        self.assertIs(caught.exception, previous)
        self.assertEqual(attempts, [1])


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL")
    and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires installed owner-initialized disposable PostgreSQL and Anvil",
)
class RealPostgreSQLDisconnectionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_killed_writer_backend_recovers_with_original_reader_and_journal(self):
        """The RED is an actual failed recovery decision, not a missing symbol."""
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]

        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def chain(method, params):
                reply = await http.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                reply.raise_for_status()
                doc = reply.json()
                self.assertNotIn("error", doc)
                return doc["result"]

            genesis = await chain("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            await chain("evm_mine", [])
            height = int((await chain("eth_getBlockByNumber", ["latest", False]))["number"], 16)

        async def progress():
            async with await AsyncConnection.connect(reader_dsn, connect_timeout=5) as sql:
                return await (await sql.execute(
                    "SELECT last_block, revision, generation, source_id, blocked_reason "
                    "FROM conix.source_progress WHERE singleton=1"
                )).fetchone()

        before = await progress()
        self.assertEqual(before[3], catalog.source_id)
        self.assertTrue(before[0] is None or before[0] < height)

        original_rpc = RPC.call
        original_open = rpc_retry.open_writer
        original_app = app.run
        original_publish = Journal.publish_block
        pending = asyncio.Event()
        release = asyncio.Event()
        interrupted = []
        applications = []
        published = []
        retired = []
        active_writers = 0

        async def hold_rpc(rpc, method, params, *, deadline):
            if method == "eth_getLogs" and not interrupted:
                interrupted.append(method)
                pending.set()
                await release.wait()
            return await original_rpc(rpc, method, params, deadline=deadline)

        @asynccontextmanager
        async def observed_open(**kwargs):
            nonlocal active_writers
            selected = None
            try:
                async with original_open(**kwargs) as writer:
                    self.assertEqual(active_writers, 0, "Writer was readmitted before prior retirement.")
                    self.assertIs(writer.journal.catalog, catalog)
                    selected = writer
                    active_writers += 1
                    yield writer
            finally:
                if selected is not None:
                    self.assertTrue(selected.journal.connection.closed,
                                    "An old SQL session remained open at retirement.")
                    active_writers -= 1
                    retired.append(selected)

        async def observed_app(*args, **kwargs):
            applications.append(args)
            return await original_app(*args, **kwargs)

        async def observed_publication(journal, *, block, events):
            result = await original_publish(journal, block=block, events=events)
            published.append(block.number)
            return result

        stop = asyncio.Event()
        with (
            patch.object(RPC, "call", hold_rpc),
            patch.object(rpc_retry, "open_writer", observed_open),
            patch.object(rpc_retry.app, "run", observed_app),
            patch.object(Journal, "publish_block", observed_publication),
        ):
            task = asyncio.create_task(
                database.supervise_with_database_recovery(catalog, dsn, stop),
            )
            try:
                await asyncio.wait_for(pending.wait(), timeout=15)

                # The original writer acquired its SQL lock before any RPC
                # call. Kill only that disposable writer's backend. This is a
                # real PostgreSQL interruption, not a simulated Python error.
                async with await AsyncConnection.connect(dsn, connect_timeout=5) as observer:
                    rows = await (await observer.execute(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE usename = current_user AND datname=current_database() "
                        "AND pid <> pg_backend_pid()"
                    )).fetchall()
                    self.assertEqual(len(rows), 1, "Could not isolate the test writer backend.")
                    killed = await (await observer.execute(
                        "SELECT pg_terminate_backend(%s)", (rows[0][0],),
                    )).fetchone()
                    self.assertEqual(killed, (True,))

                self.assertEqual(await progress(), before)
                release.set()

                async with asyncio.timeout(25):
                    while True:
                        if task.done():
                            await task
                            self.fail("Database recovery exited without resuming the installed indexer.")
                        current = await progress()
                        if current[0] is not None and current[0] >= height:
                            break
                        await asyncio.sleep(0.05)
            finally:
                stop.set()
                release.set()
                if not task.done():
                    await asyncio.wait_for(task, timeout=10)

        after = await progress()
        self.assertEqual(after[3], catalog.source_id)
        self.assertEqual(after[2], before[2], "Ordinary SQL disconnect must not change generation.")
        self.assertIsNone(after[4])
        self.assertGreaterEqual(after[0], height)
        self.assertEqual(active_writers, 0)
        self.assertEqual(len(applications), 2, "Expected a new original application session.")
        self.assertEqual(len(retired), 2)
        self.assertIn(height, published)

        # A fresh reader must observe the original journal's durable prefix.
        async with await AsyncConnection.connect(reader_dsn, connect_timeout=5) as sql:
            rows = await (await sql.execute(
                "SELECT number FROM conix.blocks WHERE number > %s AND number <= %s "
                "ORDER BY number", (before[0] or catalog.chain.start_block - 1, height),
            )).fetchall()
        self.assertEqual([number for (number,) in rows], list(range(
            (before[0] + 1) if before[0] is not None else catalog.chain.start_block,
            height + 1,
        )))


if __name__ == "__main__":
    unittest.main()
