"""Publication-specific uncertainty resumes the original application without SQL replay."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from conix.app import RunOutcome
from conix.journal import (
    CommitOutcomeUnknown, HistoryConflict, PublicationAttempt, PublicationObservation,
    PublicationOutcomeUnknown, StorageUnavailable, WriterBusy,
)
from conix.resilience import publication

SOURCE = SimpleNamespace(limits=SimpleNamespace(operation_timeout_seconds=1))


def uncertain():
    """Unit policy evidence is opaque; real tests supply journal-created context."""
    return PublicationOutcomeUnknown(PublicationAttempt(None, None, ()))


class PublicationPolicyContracts(unittest.IsolatedAsyncioTestCase):
    async def test_publication_uncertainty_reopens_only_after_original_retirement(self):
        """No second SQL inspection, no reused writer, one fresh application."""
        order = []
        failure = uncertain()

        async def application(*_args):
            order.append("application")
            try:
                if order.count("application") == 1:
                    raise failure
                self.assertEqual(order, ["application", "retired", "application"])
                return RunOutcome.STOPPED_AFTER_ADVANCE
            finally:
                order.append("retired")

        with patch.object(publication, "supervise_with_database_recovery", application):
            outcome = await publication.supervise_with_publication_recovery(
                SOURCE, None, asyncio.Event(),
            )
        self.assertIs(outcome, RunOutcome.STOPPED_AFTER_ADVANCE)
        self.assertEqual(order, ["application", "retired", "application", "retired"])

    async def test_nonpublication_failures_do_not_restart(self):
        """Uncertain repair, unclassified SQL, timeout and cleanup stay fatal."""
        for failure in (
            CommitOutcomeUnknown("repair"), StorageUnavailable("no provenance"),
            TimeoutError(), OSError("cleanup"),
        ):
            with self.subTest(failure=type(failure).__name__):
                calls = []

                async def application(*_args):
                    calls.append(True)
                    raise failure

                with patch.object(publication, "supervise_with_database_recovery", application):
                    with self.assertRaises(type(failure)) as caught:
                        await publication.supervise_with_publication_recovery(
                            SOURCE, None, asyncio.Event(),
                        )
                self.assertIs(caught.exception, failure)
                self.assertEqual(calls, [True])

    async def test_new_session_admission_errors_propagate_without_another_retry(self):
        """Fresh original admission retains source, lock and storage authority."""
        for failure in (
            WriterBusy("owned"), StorageUnavailable("database unavailable"),
            HistoryConflict("retained source changed"), OSError("retirement uncertain"),
        ):
            with self.subTest(failure=type(failure).__name__):
                calls = []
                uncertainty = uncertain()

                async def application(*_args):
                    calls.append(True)
                    if len(calls) == 1:
                        raise uncertainty
                    raise failure

                with patch.object(publication, "supervise_with_database_recovery", application):
                    with self.assertRaises(type(failure)) as caught:
                        await publication.supervise_with_publication_recovery(
                            SOURCE, None, asyncio.Event(),
                        )
                self.assertIs(caught.exception, failure)
                self.assertEqual(calls, [True, True])

    async def test_failed_retirement_never_grants_recovery(self):
        """A cleanup failure overrides publication uncertainty before admission."""
        calls = []
        cleanup = OSError("failed connection retirement")

        async def application(*_args):
            calls.append(True)
            try:
                raise uncertain()
            finally:
                raise cleanup

        with patch.object(publication, "supervise_with_database_recovery", application):
            with self.assertRaises(OSError) as caught:
                await publication.supervise_with_publication_recovery(
                    SOURCE, None, asyncio.Event(),
                )
        self.assertIs(caught.exception, cleanup)
        self.assertEqual(calls, [True])

    async def test_outer_budget_survives_reentry(self):
        """Nested D1/C/B work does not reset the two D2 recovery attempts."""
        calls = []
        failures = [uncertain() for _ in range(3)]

        async def application(*_args):
            failure = failures[len(calls)]
            calls.append(True)
            raise failure

        with patch.object(publication, "supervise_with_database_recovery", application):
            with self.assertRaises(PublicationOutcomeUnknown) as caught:
                await publication.supervise_with_publication_recovery(
                    SOURCE, None, asyncio.Event(),
                )
        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(len(calls), 3)

    async def test_stop_at_nested_entry_preserves_original_uncertainty(self):
        """A no-op return is not evidence that resumed indexing ran."""
        calls = []
        stop = asyncio.Event()
        failure = uncertain()

        async def application(*_args):
            calls.append(True)
            if len(calls) == 1:
                raise failure
            stop.set()
            return RunOutcome.STOPPED_BEFORE_ADVANCE

        with patch.object(publication, "supervise_with_database_recovery", application):
            with self.assertRaises(PublicationOutcomeUnknown) as caught:
                await publication.supervise_with_publication_recovery(
                    SOURCE, None, stop,
                )
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 2)

    async def test_stop_or_cancellation_during_recovery_wait_prevents_restart(self):
        """Stop retains uncertainty; task cancellation remains cancellation."""
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                calls = []
                stop = asyncio.Event()
                entered = asyncio.Event()
                failure = uncertain()

                async def application(*_args):
                    calls.append(True)
                    entered.set()
                    raise failure

                with patch.object(publication, "supervise_with_database_recovery", application):
                    task = asyncio.create_task(publication.supervise_with_publication_recovery(
                        SOURCE, None, stop,
                    ))
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    await asyncio.sleep(0)
                    if cancel:
                        task.cancel()
                    else:
                        stop.set()
                    with self.assertRaises(
                        asyncio.CancelledError if cancel else PublicationOutcomeUnknown,
                    ):
                        await task
                self.assertEqual(calls, [True])

    async def test_real_nested_supervisors_keep_outer_budget_and_forward_outcomes(self):
        """D1 retries and D2 recovery interleave without resetting the D2 budget."""
        import psycopg
        from conix.resilience import rpc_retry

        failures = [uncertain() for _ in range(3)]
        calls = []

        async def original_app(*_args, **_kwargs):
            index = len(calls)
            calls.append(True)
            if index % 2 == 0:
                error = StorageUnavailable("qualified disconnect")
                error.__cause__ = psycopg.errors.ConnectionFailure("fixture")
                raise error
            raise failures[index // 2]

        with patch.object(rpc_retry.app, "run", original_app):
            with self.assertRaises(PublicationOutcomeUnknown) as caught:
                await publication.supervise_with_publication_recovery(
                    SOURCE, None, asyncio.Event(),
                )
        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(len(calls), 6)

        calls.clear()
        stop = asyncio.Event()

        async def stopped_app(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise failures[0]
            stop.set()
            return RunOutcome.STOPPED_BEFORE_ADVANCE

        with patch.object(rpc_retry.app, "run", stopped_app):
            with self.assertRaises(PublicationOutcomeUnknown) as caught:
                await publication.supervise_with_publication_recovery(SOURCE, None, stop)
        self.assertIs(caught.exception, failures[0])
        self.assertEqual(len(calls), 2)


# These imports remain test-only; the installed runtime has no fixture dependency.
import os

import httpx
from psycopg import AsyncConnection

from anvil_catalog import catalog_for_anvil
from abi_golden_vectors import ERC20
from pg_commit_reply import without_commit_reply
from conix import app
from conix.core import open_writer
from conix.journal import Journal, open_journal
from conix.reader import open_reader


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires real disposable Anvil/PostgreSQL",
)
class RealPublicationRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    async def test_actual_commit_outcomes_resume_original_application_and_reader(self):
        """Both real durable outcomes resume ordinary indexing without reconciliation."""
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def rpc(method, params):
                reply = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                result = reply.json()
                self.assertNotIn("error", result)
                return result["result"]

            genesis = await rpc("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            await rpc("evm_mine", [])
            async with open_writer(catalog=catalog, database_dsn=dsn, rpc_url=url) as writer:
                while await writer.advance_one() is not None:
                    pass

            token = "0x" + "11" * 20
            # Emit the existing literal ERC-20 golden record, without a
            # contract-specific scanner or a payment workflow substitute.
            runtime = (b"\x7f" + ERC20.data + b"\x60\x00\x52"
                       + b"".join(b"\x7f" + topic for topic in reversed(ERC20.topics))
                       + b"\x60\x20\x60\x00\xa3\x00")
            await rpc("anvil_setCode", [token, "0x" + runtime.hex()])
            account = (await rpc("eth_accounts", []))[0]
            for committed in (False, True):
                async with open_reader(catalog=catalog, database_dsn=reader_dsn, rpc_url=url) as reader:
                    before = await reader.checkpoint()
                    prefix = await reader.events(checkpoint=before, contract="token", event="Transfer(address,address,uint256)", limit=catalog.limits.max_page_size)
                    self.assertTrue(prefix.complete)
                transaction = await rpc("eth_sendTransaction", [{"from": account, "to": token, "gas": "0x100000"}])
                receipt = await rpc("eth_getTransactionReceipt", [transaction])
                self.assertEqual(receipt["status"], "0x1")
                self.assertEqual(len(receipt["logs"]), 1)
                candidate_height = int(receipt["blockNumber"], 16)
                self.assertEqual(candidate_height, before.block.number + 1)
                stop = asyncio.Event()
                sessions, published = [], []
                execute = AsyncConnection.execute
                application = publication.supervise_with_database_recovery
                publish = Journal.publish_block
                report = app.report
                injected = False

                async def unsent(connection, statement, *args, **kwargs):
                    nonlocal injected
                    if statement == "COMMIT" and not injected:
                        injected = True
                        raise ConnectionError("test COMMIT not sent")
                    return await execute(connection, statement, *args, **kwargs)

                async def original_publish(at_journal, **kwargs):
                    result = await publish(at_journal, **kwargs)
                    published.append(result.last_block.number)
                    return result

                def progressed(progress):
                    report(progress)
                    if progress.last_block.number >= candidate_height + 1:
                        stop.set()

                async def exercise(first_dsn):
                    async def original_application(source, _dsn, event):
                        sessions.append(True)
                        try:
                            return await application(
                                source, first_dsn if len(sessions) == 1 else dsn, event,
                            )
                        except PublicationOutcomeUnknown:
                            self.assertEqual(len(sessions), 1)
                            # The failed session has retired. A NEW original
                            # application will acquire this next block's evidence.
                            await rpc("evm_mine", [])
                            raise

                    with (
                        patch.object(publication, "supervise_with_database_recovery", original_application),
                        patch.object(Journal, "publish_block", original_publish),
                        patch.object(app, "report", progressed),
                    ):
                        return await asyncio.wait_for(
                            publication.supervise_with_publication_recovery(catalog, dsn, stop),
                            timeout=25,
                        )

                if committed:
                    async with without_commit_reply(dsn) as (relay_dsn, dropped):
                        outcome = await exercise(relay_dsn)
                        await dropped
                else:
                    with patch.object(AsyncConnection, "execute", unsent):
                        outcome = await exercise(dsn)
                self.assertIs(outcome, RunOutcome.STOPPED_AFTER_ADVANCE)
                self.assertEqual(len(sessions), 2)
                self.assertIn(candidate_height + 1, published)
                self.assertEqual(published.count(candidate_height), 0 if committed else 1)
                async with open_reader(catalog=catalog, database_dsn=reader_dsn, rpc_url=url) as reader:
                    after = await reader.checkpoint()
                    page = await reader.events(checkpoint=after, contract="token", event="Transfer(address,address,uint256)", limit=catalog.limits.max_page_size)
                self.assertEqual(after.block.number, candidate_height + 1)
                self.assertEqual(after.generation, before.generation)
                self.assertEqual(after.revision, before.revision + 2)
                self.assertTrue(page.complete)
                self.assertEqual(page.events[:-1], prefix.events)
                self.assertEqual(sum(event.transaction_hash.hex() == transaction[2:] for event in page.events), 1)


    async def test_intervening_original_writer_progress_does_not_block_recovery(self):
        """A successful authorized advance is not a reason to stop D2."""
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]

        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def rpc(method, params):
                response = await http.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                response.raise_for_status()
                result = response.json()
                self.assertNotIn("error", result)
                return result["result"]

            genesis = await rpc("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            await rpc("evm_mine", [])
            async with open_writer(catalog=catalog, database_dsn=dsn, rpc_url=url) as writer:
                while await writer.advance_one() is not None:
                    pass

            # Use the same real selected ERC-20 event as the normal D2 witness.
            token = "0x" + "11" * 20
            runtime = (b"\x7f" + ERC20.data + b"\x60\x00\x52"
                       + b"".join(b"\x7f" + topic for topic in reversed(ERC20.topics))
                       + b"\x60\x20\x60\x00\xa3\x00")
            await rpc("anvil_setCode", [token, "0x" + runtime.hex()])
            account = (await rpc("eth_accounts", []))[0]

            async with open_reader(catalog=catalog, database_dsn=reader_dsn, rpc_url=url) as reader:
                before = await reader.checkpoint()
                prefix = await reader.events(
                    checkpoint=before, contract="token",
                    event="Transfer(address,address,uint256)", limit=catalog.limits.max_page_size,
                )
                self.assertTrue(prefix.complete)

            transaction = await rpc("eth_sendTransaction", [{
                "from": account, "to": token, "gas": "0x100000",
            }])
            receipt = await rpc("eth_getTransactionReceipt", [transaction])
            self.assertEqual(receipt["status"], "0x1")
            self.assertEqual(len(receipt["logs"]), 1)
            candidate_height = int(receipt["blockNumber"], 16)
            self.assertEqual(candidate_height, before.block.number + 1)

            original_application = publication.supervise_with_database_recovery
            original_reconcile = Journal.reconcile_publication
            original_report = app.report
            attempts, supervisor_inspections, intervening_advances = [], [], []
            stop = asyncio.Event()

            async def inspected(journal, attempt):
                supervisor_inspections.append(True)
                return await original_reconcile(journal, attempt)

            def report(progress):
                original_report(progress)
                if progress.last_block.number >= candidate_height + 2:
                    stop.set()

            async with without_commit_reply(dsn) as (relay_dsn, dropped):
                async def supervised_application(source, _dsn, signal):
                    attempts.append(True)
                    try:
                        return await original_application(
                            source, relay_dsn if len(attempts) == 1 else dsn, signal,
                        )
                    except PublicationOutcomeUnknown as failure:
                        self.assertEqual(len(attempts), 1)
                        self.assertEqual(failure.attempt.block.number, candidate_height)
                        await dropped

                        # The original failed app has retired. A different,
                        # authorized original writer must reacquire its lock,
                        # inspect actual progress and publish the next block.
                        await rpc("evm_mine", [])
                        async with open_writer(
                            catalog=catalog, database_dsn=dsn, rpc_url=url,
                        ) as writer:
                            progress = await writer.advance_one()
                            self.assertEqual(progress.last_block.number, candidate_height + 1)
                            intervening_advances.append(progress)
                            self.assertIsNone(await writer.advance_one())

                        # Verify why the old strict gate refuses: legitimate
                        # progress has moved past the attempted revision.
                        # This inspection is test-only, not a D2 prerequisite.
                        async with open_journal(catalog=catalog, database_dsn=dsn) as journal:
                            self.assertIs(
                                await original_reconcile(journal, failure.attempt),
                                PublicationObservation.OTHER_DURABLE_STATE,
                            )
                        await rpc("evm_mine", [])
                        raise

                with (
                    patch.object(publication, "supervise_with_database_recovery", supervised_application),
                    patch.object(Journal, "reconcile_publication", inspected),
                    patch.object(app, "report", report),
                ):
                    outcome = await asyncio.wait_for(
                        publication.supervise_with_publication_recovery(catalog, dsn, stop),
                        timeout=30,
                    )
                await dropped

            self.assertIs(outcome, RunOutcome.STOPPED_AFTER_ADVANCE)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(len(intervening_advances), 1)
            self.assertEqual(supervisor_inspections, [],
                             "D2 must not require an attempt-specific journal observation.")

            async with open_reader(catalog=catalog, database_dsn=reader_dsn, rpc_url=url) as reader:
                after = await reader.checkpoint()
                page = await reader.events(
                    checkpoint=after, contract="token",
                    event="Transfer(address,address,uint256)", limit=catalog.limits.max_page_size,
                )
            self.assertEqual(after.block.number, candidate_height + 2)
            self.assertEqual(after.generation, before.generation)
            self.assertEqual(after.revision, before.revision + 3)
            self.assertTrue(page.complete)
            self.assertEqual(page.events[:-1], prefix.events)
            self.assertEqual(
                sum(event.transaction_hash.hex() == transaction[2:] for event in page.events),
                1,
            )
