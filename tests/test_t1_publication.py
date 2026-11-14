"""Recovery policy consumes the original journal's observations, never SQL."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from conix.app import RunOutcome
from conix.journal import (
    CommitOutcomeUnknown, PublicationAttempt, PublicationObservation,
    PublicationOutcomeUnknown, StorageUnavailable, WriterBusy,
)
from conix.resilience import publication

SOURCE = SimpleNamespace(limits=SimpleNamespace(operation_timeout_seconds=1))


def uncertain():
    """Unit policy evidence is opaque; real tests supply journal-created context."""
    return PublicationOutcomeUnknown(PublicationAttempt(None, None, ()))


class PublicationPolicyContracts(unittest.IsolatedAsyncioTestCase):
    async def test_matching_observation_after_retirement_then_original_application(self):
        """Both matching observations permit another complete original invocation."""
        for observation in (
            PublicationObservation.ORIGINAL_BASIS_PRESERVED,
            PublicationObservation.EXPECTED_EFFECT_PRESENT,
        ):
            order = []
            failure = uncertain()

            async def application(*_args):
                order.append("application")
                if order.count("application") == 1:
                    order.append("application retired")
                    raise failure
                return RunOutcome.STOPPED_AFTER_ADVANCE

            async def reconcile(attempt):
                self.assertIs(attempt, failure.attempt)
                order.append("inspect")
                return observation

            @asynccontextmanager
            async def session(**_kwargs):
                self.assertEqual(order[-1], "application retired")
                order.append("journal")
                try:
                    yield SimpleNamespace(reconcile_publication=reconcile)
                finally:
                    order.append("inspection retired")

            with patch.object(publication, "supervise_with_database_recovery", application), patch.object(publication, "open_journal", session):
                result = await publication.supervise_with_publication_recovery(SOURCE, None, asyncio.Event())
            self.assertIs(result, RunOutcome.STOPPED_AFTER_ADVANCE)
            self.assertEqual(order, ["application", "application retired", "journal", "inspect", "inspection retired", "application"])

    async def test_nonpublication_failures_never_inspect_or_restart(self):
        """Repair, unclassified SQL, timeout and cleanup uncertainty stay terminal."""
        for failure in (CommitOutcomeUnknown("repair"), StorageUnavailable("no provenance"), TimeoutError(), OSError("cleanup")):
            async def application(*_args):
                raise failure
            with patch.object(publication, "supervise_with_database_recovery", application):
                with patch.object(publication, "open_journal") as opened:
                    with self.assertRaises(type(failure)) as caught:
                        await publication.supervise_with_publication_recovery(SOURCE, None, asyncio.Event())
                    self.assertIs(caught.exception, failure)
                    opened.assert_not_called()

    async def test_other_state_and_failed_inspection_or_cleanup_stop_recovery(self):
        """Only a completed matching observation with retired inspection may restart."""
        for secondary in (None, StorageUnavailable("read failed"), WriterBusy("owned"), OSError("close failed")):
            calls = []
            failure = uncertain()

            async def application(*_args):
                calls.append(True)
                raise failure

            async def reconcile(_attempt):
                if isinstance(secondary, StorageUnavailable):
                    raise secondary
                return PublicationObservation.OTHER_DURABLE_STATE if secondary is None else PublicationObservation.EXPECTED_EFFECT_PRESENT

            @asynccontextmanager
            async def session(**_kwargs):
                if isinstance(secondary, WriterBusy):
                    raise secondary
                try:
                    yield SimpleNamespace(reconcile_publication=reconcile)
                finally:
                    if isinstance(secondary, OSError):
                        raise secondary

            with patch.object(publication, "supervise_with_database_recovery", application), patch.object(publication, "open_journal", session):
                expected = failure if secondary is None else secondary
                with self.assertRaises(type(expected)) as caught:
                    await publication.supervise_with_publication_recovery(SOURCE, None, asyncio.Event())
                self.assertIs(caught.exception, expected)
            self.assertEqual(calls, [True])

    async def test_outer_budget_is_not_reset_by_nested_application_reentry(self):
        """Repeated publication failures exhaust two recoveries, regardless of inner work."""
        calls = []
        failures = [uncertain() for _ in range(3)]

        async def application(*_args):
            failure = failures[len(calls)]
            calls.append(True)
            raise failure

        async def reconcile(_attempt):
            return PublicationObservation.EXPECTED_EFFECT_PRESENT

        @asynccontextmanager
        async def session(**_kwargs):
            yield SimpleNamespace(reconcile_publication=reconcile)

        with patch.object(publication, "supervise_with_database_recovery", application), patch.object(publication, "open_journal", session):
            with self.assertRaises(PublicationOutcomeUnknown) as caught:
                await publication.supervise_with_publication_recovery(SOURCE, None, asyncio.Event())
        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(len(calls), 3)

    async def test_stop_at_nested_entry_preserves_original_uncertainty(self):
        """A nested no-op return cannot falsely settle the previous failure."""
        calls = []
        stop = asyncio.Event()
        failure = uncertain()

        async def application(*_args):
            calls.append(True)
            if len(calls) == 1:
                raise failure
            stop.set()
            return RunOutcome.STOPPED_BEFORE_ADVANCE

        async def reconcile(_attempt):
            return PublicationObservation.ORIGINAL_BASIS_PRESERVED

        @asynccontextmanager
        async def session(**_kwargs):
            yield SimpleNamespace(reconcile_publication=reconcile)

        with patch.object(publication, "supervise_with_database_recovery", application), patch.object(publication, "open_journal", session):
            with self.assertRaises(PublicationOutcomeUnknown) as caught:
                await publication.supervise_with_publication_recovery(SOURCE, None, stop)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 2)

    async def test_stop_or_cancellation_during_recovery_wait_prevents_restart(self):
        """Stop retains the uncertainty; task cancellation remains cancellation."""
        for cancel in (False, True):
            calls = []
            stop = asyncio.Event()
            inspecting = asyncio.Event()
            failure = uncertain()

            async def application(*_args):
                calls.append(True)
                raise failure

            async def reconcile(_attempt):
                return PublicationObservation.EXPECTED_EFFECT_PRESENT

            @asynccontextmanager
            async def session(**_kwargs):
                yield SimpleNamespace(reconcile_publication=reconcile)
                inspecting.set()

            with patch.object(publication, "supervise_with_database_recovery", application), patch.object(publication, "open_journal", session):
                task = asyncio.create_task(publication.supervise_with_publication_recovery(SOURCE, None, stop))
                await asyncio.wait_for(inspecting.wait(), timeout=2)
                if cancel:
                    task.cancel()
                else:
                    stop.set()
                with self.assertRaises(asyncio.CancelledError if cancel else PublicationOutcomeUnknown):
                    await task
            self.assertEqual(calls, [True])

    async def test_stalled_inspection_times_out_without_restart(self):
        """Transport silence cannot outlive the recovery observation deadline."""
        calls = []
        closed = []
        failure = uncertain()

        async def application(*_args):
            calls.append(True)
            raise failure

        async def reconcile(_attempt):
            await asyncio.Future()

        @asynccontextmanager
        async def session(**_kwargs):
            try:
                yield SimpleNamespace(reconcile_publication=reconcile)
            finally:
                closed.append(True)

        source = SimpleNamespace(limits=SimpleNamespace(operation_timeout_seconds=0.02))
        with patch.object(publication, "supervise_with_database_recovery", application), patch.object(publication, "open_journal", session):
            with self.assertRaises(TimeoutError):
                await publication.supervise_with_publication_recovery(source, None, asyncio.Event())
        self.assertEqual(calls, [True])
        self.assertEqual(closed, [True])

    async def test_real_nested_supervisors_keep_outer_budget_and_forward_outcomes(self):
        """D1 retries and D2 recovery interleave without resetting the outer budget."""
        import psycopg
        from conix.resilience import rpc_retry

        failures = [uncertain() for _ in range(3)]
        calls, inspections = [], []

        async def original_app(*_args, **_kwargs):
            index = len(calls)
            calls.append(True)
            if index % 2 == 0:
                error = StorageUnavailable("qualified disconnect")
                error.__cause__ = psycopg.errors.ConnectionFailure("fixture")
                raise error
            raise failures[index // 2]

        async def reconcile(_attempt):
            inspections.append(True)
            return PublicationObservation.EXPECTED_EFFECT_PRESENT

        @asynccontextmanager
        async def session(**_kwargs):
            yield SimpleNamespace(reconcile_publication=reconcile)

        with patch.object(rpc_retry.app, "run", original_app), patch.object(publication, "open_journal", session):
            with self.assertRaises(PublicationOutcomeUnknown) as caught:
                await publication.supervise_with_publication_recovery(SOURCE, None, asyncio.Event())
        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(inspections), 2)

        calls.clear()
        stop = asyncio.Event()

        async def stopped_app(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise failures[0]
            stop.set()
            return RunOutcome.STOPPED_BEFORE_ADVANCE

        with patch.object(rpc_retry.app, "run", stopped_app), patch.object(publication, "open_journal", session):
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
from conix.journal import Journal
from conix.reader import open_reader


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires real disposable Anvil/PostgreSQL",
)
class RealPublicationRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    async def test_actual_commit_outcomes_resume_original_application_and_reader(self):
        """Both durable outcomes pass core observation before a fresh real advance."""
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
                sessions, observations, published = [], [], []
                execute = AsyncConnection.execute
                application = publication.supervise_with_database_recovery
                observe = Journal.reconcile_publication
                publish = Journal.publish_block
                report = app.report
                injected = False

                async def unsent(connection, statement, *args, **kwargs):
                    nonlocal injected
                    if statement == "COMMIT" and not injected:
                        injected = True
                        raise ConnectionError("test COMMIT not sent")
                    return await execute(connection, statement, *args, **kwargs)

                async def observed(at_journal, attempt):
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(attempt.block.number, candidate_height)
                    result = await observe(at_journal, attempt)
                    observations.append(result)
                    # New chain evidence must be acquired after observation;
                    # the next normal advance also processes a later block.
                    await rpc("evm_mine", [])
                    return result

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
                        return await application(source, first_dsn if len(sessions) == 1 else dsn, event)

                    with patch.object(publication, "supervise_with_database_recovery", original_application), patch.object(Journal, "reconcile_publication", observed), patch.object(Journal, "publish_block", original_publish), patch.object(app, "report", progressed):
                        return await asyncio.wait_for(publication.supervise_with_publication_recovery(catalog, dsn, stop), timeout=25)

                if committed:
                    async with without_commit_reply(dsn) as (relay_dsn, dropped):
                        outcome = await exercise(relay_dsn)
                        await dropped
                else:
                    with patch.object(AsyncConnection, "execute", unsent):
                        outcome = await exercise(dsn)
                self.assertIs(outcome, RunOutcome.STOPPED_AFTER_ADVANCE)
                self.assertEqual(len(sessions), 2)
                self.assertEqual(observations, [PublicationObservation.EXPECTED_EFFECT_PRESENT if committed else PublicationObservation.ORIGINAL_BASIS_PRESERVED])
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
