"""T1-C2: fresh repair admission and supervised restart of the Tier-0 indexer.

Selection is not authority to delete a suffix. These tests demand a new
canonicality check, unchanged durable basis, original checked repair,
complete session retirement and a fresh ordinary application session.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from psycopg import AsyncConnection

from abi_golden_vectors import ERC20, TRANSFER_SIGNATURE
from anvil_catalog import catalog_for_anvil
from conix import app
from conix.core import BranchConflict, open_writer
from conix.journal import (
    Block, CommitOutcomeUnknown, HistoryConflict, Journal, Progress,
    REORGANIZATION_BLOCKED_REASON, RetainedAncestry, StorageUnavailable,
)
from conix.reader import CursorInvalid, open_reader
from conix.resilience import reorg, rpc_retry, supervisor
from conix.rpc import RpcProtocolError, RpcUnavailable


def block(number):
    return Block(
        number=number, hash=bytes([number]) * 32,
        parent_hash=bytes([number - 1]) * 32,
        timestamp=1700000000 + number,
    )


class ReorganizationRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.basis = Progress(
            source_id=b"s" * 32, generation=uuid4(),
            history_start=1, revision=20,
            last_block=block(9), blocked_reason=None,
        )
        self.blocks = (block(9), block(8), block(7))
        self.conflict = replace(block(9), hash=b"x" * 32)
        self.observed = []
        self.repair_calls = []
        self.journal = SimpleNamespace(
            basis=self.basis,
            retained_ancestry=AsyncMock(return_value=RetainedAncestry(
                progress=self.basis, blocks=self.blocks,
            )),
            inspect=AsyncMock(return_value=self.basis),
        )
        self.writer = SimpleNamespace(
            catalog=SimpleNamespace(limits=SimpleNamespace(
                max_evidence_blocks=3, operation_timeout_seconds=5,
            )),
            journal=self.journal,
            repair_to=AsyncMock(return_value=replace(
                self.basis, last_block=block(8), revision=21,
                generation=uuid4(),
            )),
        )
        self.source = object()
        self.dsn = "private-DSN"
        self.stop = asyncio.Event()

    def set_observations(self, observations):
        pending = {height: list(values) for height, values in observations.items()}

        async def read(height, deadline):
            self.observed.append((height, deadline))
            expected = pending[height].pop(0)
            if expected is None:
                return None
            return SimpleNamespace(header=expected)

        self.writer._block = read

    @asynccontextmanager
    async def opened(self, **kwargs):
        self.assertEqual(kwargs, {
            "catalog": self.source,
            "database_dsn": self.dsn,
            "rpc_url": self.source.chain.rpc_url,
        })
        yield self.writer

    def source_catalog(self):
        self.source = SimpleNamespace(
            chain=SimpleNamespace(rpc_url="http://selected.invalid"),
            limits=self.writer.catalog.limits,
        )
        self.writer.catalog = self.source
        return self.source

    async def invoke(self, *, blocked_only=False):
        catalog = self.source_catalog()
        with patch.object(reorg, "open_writer", self.opened):
            return await reorg.recover_reorganization(
                catalog, self.dsn, self.stop, blocked_only=blocked_only,
            )

    async def test_real_repair_authority_is_invoked_only_after_fresh_checks(self):
        self.set_observations({
            9: [self.conflict, self.conflict],
            8: [block(8), block(8)],
        })
        self.assertTrue(await self.invoke())
        self.assertEqual([height for height, _ in self.observed], [9, 8, 8, 9])
        self.assertEqual(self.journal.inspect.await_count, 2)
        self.writer.repair_to.assert_awaited_once_with(ancestor=block(8))
        self.assertEqual(len({deadline for _, deadline in self.observed[:2]}), 1)
        self.assertEqual(len({deadline for _, deadline in self.observed[2:]}), 1)

    async def test_healed_fork_refuses_to_discard_still_canonical_history(self):
        self.set_observations({
            9: [self.conflict, block(9)],
            8: [block(8), block(8)],
        })
        self.assertIs(await self.invoke(), False)
        self.assertEqual([height for height, _ in self.observed], [9, 8, 8, 9])
        self.writer.repair_to.assert_not_awaited()

    async def test_ancestor_changed_after_selection_refuses_repair(self):
        self.set_observations({
            9: [self.conflict],
            8: [block(8), replace(block(8), hash=b"q" * 32)],
        })
        with self.assertRaises(BranchConflict):
            await self.invoke()
        self.writer.repair_to.assert_not_awaited()

    async def test_missing_evidence_in_final_check_is_not_reorg_proof(self):
        self.set_observations({
            9: [self.conflict],
            8: [block(8), None],
        })
        with self.assertRaises(RpcUnavailable):
            await self.invoke()
        self.writer.repair_to.assert_not_awaited()

    async def test_missing_current_tip_at_final_check_refuses_repair(self):
        self.set_observations({
            9: [self.conflict, None],
            8: [block(8), block(8)],
        })
        with self.assertRaises(RpcUnavailable):
            await self.invoke()
        self.writer.repair_to.assert_not_awaited()

    async def test_changed_basis_between_selection_and_repair_refuses_mutation(self):
        self.journal.inspect.side_effect = [
            self.basis,
            replace(self.basis, revision=self.basis.revision + 1),
        ]
        self.set_observations({
            9: [self.conflict, self.conflict],
            8: [block(8), block(8)],
        })
        with self.assertRaises(HistoryConflict):
            await self.invoke()
        self.writer.repair_to.assert_not_awaited()

    async def test_existing_blocked_history_only_allows_verified_fork(self):
        self.journal.basis = replace(
            self.basis, blocked_reason=REORGANIZATION_BLOCKED_REASON,
        )
        self.journal.retained_ancestry.return_value = RetainedAncestry(
            self.journal.basis, self.blocks,
        )
        self.journal.inspect.return_value = self.journal.basis
        self.set_observations({
            9: [self.conflict, self.conflict],
            8: [block(8), block(8)],
        })
        self.assertTrue(await self.invoke(blocked_only=True))
        self.writer.repair_to.assert_awaited_once_with(ancestor=block(8))

    async def test_unblocked_history_conflict_cannot_trigger_repair(self):
        self.set_observations({})
        self.assertIsNone(await self.invoke(blocked_only=True))
        self.assertEqual(self.observed, [])
        self.writer.repair_to.assert_not_awaited()

    async def test_unrelated_operator_hold_cannot_be_repaired_even_if_chain_forked(self):
        """An actual fork cannot clear a separate intervention-required SQL hold."""
        self.journal.basis = replace(
            self.basis, blocked_reason="uncertain SQL publication: inspect before release",
        )
        self.journal.retained_ancestry.return_value = RetainedAncestry(
            self.journal.basis, self.blocks,
        )
        self.journal.inspect.return_value = self.journal.basis
        self.set_observations({
            9: [self.conflict, self.conflict],
            8: [block(8), block(8)],
        })
        self.assertIsNone(await self.invoke(blocked_only=True))
        self.writer.repair_to.assert_not_awaited()
        self.assertEqual(self.observed, [])

    async def test_late_operator_hold_also_blocks_repair_after_branch_conflict(self):
        """Never let a prior BranchConflict override a newer durable hold."""
        self.journal.basis = replace(
            self.basis, blocked_reason="operator retained uncertain COMMIT",
        )
        self.journal.retained_ancestry.return_value = RetainedAncestry(
            self.journal.basis, self.blocks,
        )
        self.journal.inspect.return_value = self.journal.basis
        self.set_observations({
            9: [self.conflict, self.conflict],
            8: [block(8), block(8)],
        })
        self.assertIsNone(await self.invoke(blocked_only=False))
        self.writer.repair_to.assert_not_awaited()
        self.assertEqual(self.observed, [])

    async def test_blocked_but_canonical_history_is_not_silently_cleared(self):
        self.journal.basis = replace(
            self.basis, blocked_reason=REORGANIZATION_BLOCKED_REASON,
        )
        self.journal.retained_ancestry.return_value = RetainedAncestry(
            self.journal.basis, self.blocks,
        )
        self.journal.inspect.return_value = self.journal.basis
        self.set_observations({9: [block(9)]})
        self.assertIs(await self.invoke(blocked_only=True), False)
        self.assertEqual([height for height, _ in self.observed], [9])
        self.writer.repair_to.assert_not_awaited()

    async def test_stop_or_cancellation_cannot_turn_selection_into_mutation(self):
        self.stop.set()
        self.set_observations({})
        self.assertIsNone(await self.invoke())
        self.assertEqual(self.observed, [])

        self.stop.clear()
        self.set_observations({
            9: [self.conflict], 8: [block(8)],
        })
        original = self.writer._block

        async def stop_during_ancestor(height, deadline):
            value = await original(height, deadline)
            if height == 8:
                self.stop.set()
            return value

        self.writer._block = stop_during_ancestor
        self.assertIsNone(await self.invoke())
        self.writer.repair_to.assert_not_awaited()

    async def test_stop_after_final_evidence_prevents_mutation(self):
        self.set_observations({
            9: [self.conflict, self.conflict],
            8: [block(8), block(8)],
        })
        self.journal.inspect.side_effect = [
            self.basis,
            self.basis,
        ]
        original_inspect = self.journal.inspect

        async def stop_before_repair():
            observed = await original_inspect()
            if original_inspect.await_count == 2:
                self.stop.set()
            return observed

        self.journal.inspect = stop_before_repair
        self.assertIsNone(await self.invoke())
        self.writer.repair_to.assert_not_awaited()

    async def test_original_uncertain_commit_is_never_retried(self):
        self.set_observations({
            9: [self.conflict, self.conflict],
            8: [block(8), block(8)],
        })
        unknown = CommitOutcomeUnknown("COMMIT reply lost")
        self.writer.repair_to.side_effect = unknown
        with self.assertRaises(CommitOutcomeUnknown) as caught:
            await self.invoke()
        self.assertIs(caught.exception, unknown)
        self.writer.repair_to.assert_awaited_once()


class ReorganizationSupervisionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_starts_only_after_old_session_retired(self):
        calls = []
        retired = False
        error = BranchConflict("retained tip changed")
        stop = asyncio.Event()

        async def run(*args):
            nonlocal retired
            calls.append("run")
            if len(calls) == 1:
                try:
                    raise error
                finally:
                    retired = True
            stop.set()

        async def recover(*args, **kwargs):
            self.assertTrue(retired)
            self.assertEqual(kwargs["blocked_only"], False)
            calls.append("repair")
            return True

        with (
            patch.object(supervisor, "supervise", run),
            patch.object(reorg, "recover_reorganization", recover),
        ):
            await reorg.supervise_with_reorg_recovery(object(), object(), stop)
        self.assertEqual(calls, ["run", "repair", "run"])

    async def test_temporary_mismatch_may_restart_without_repair(self):
        stop = asyncio.Event()
        attempts = []

        async def run(*args):
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise BranchConflict("candidate changed before publication")
            stop.set()

        with (
            patch.object(supervisor, "supervise", run),
            patch.object(reorg, "recover_reorganization", AsyncMock(return_value=False)) as recover,
        ):
            await reorg.supervise_with_reorg_recovery(object(), object(), stop)
        self.assertEqual(len(attempts), 2)
        recover.assert_awaited_once()

    async def test_blocked_history_must_be_confirmed_and_repaired(self):
        error = HistoryConflict("blocked history")
        calls = []
        stop = asyncio.Event()

        async def run(*args):
            calls.append(len(calls))
            if len(calls) == 1:
                raise error
            stop.set()

        with (
            patch.object(supervisor, "supervise", run),
            patch.object(reorg, "recover_reorganization", AsyncMock(return_value=True)) as recover,
        ):
            await reorg.supervise_with_reorg_recovery(object(), object(), stop)
        self.assertEqual(len(calls), 2)
        self.assertTrue(recover.await_args.kwargs["blocked_only"])

    async def test_unrelated_history_conflict_does_not_restart(self):
        failure = HistoryConflict("unrelated invalid SQL history")
        attempts = []
        async def run(*args):
            attempts.append(True)
            raise failure

        with (
            patch.object(supervisor, "supervise", run),
            patch.object(reorg, "recover_reorganization", AsyncMock(return_value=None)) as recover,
        ):
            with self.assertRaises(HistoryConflict) as caught:
                await reorg.supervise_with_reorg_recovery(object(), object(), asyncio.Event())
        self.assertIs(caught.exception, failure)
        self.assertEqual(attempts, [True])
        self.assertTrue(recover.await_args.kwargs["blocked_only"])

    async def test_canonical_but_blocked_history_remains_blocked(self):
        failure = HistoryConflict("blocked history")
        with (
            patch.object(supervisor, "supervise", AsyncMock(side_effect=failure)),
            patch.object(reorg, "recover_reorganization", AsyncMock(return_value=False)) as recover,
        ):
            with self.assertRaises(HistoryConflict) as caught:
                await reorg.supervise_with_reorg_recovery(object(), object(), asyncio.Event())
        self.assertIs(caught.exception, failure)
        recover.assert_awaited_once()

    async def test_bounded_retry_budget_preserves_latest_failure(self):
        failures = [BranchConflict(str(i)) for i in range(3)]
        attempted = []
        async def run(*args):
            attempted.append(len(attempted))
            raise failures[len(attempted) - 1]

        with (
            patch.object(supervisor, "supervise", run),
            patch.object(reorg, "recover_reorganization", AsyncMock(return_value=True)) as recover,
        ):
            with self.assertRaises(BranchConflict) as caught:
                await reorg.supervise_with_reorg_recovery(object(), object(), asyncio.Event())
        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(attempted, [0, 1, 2])
        self.assertEqual(recover.await_count, 2)

    async def test_other_failures_and_uncertain_retirement_are_not_recovery_grants(self):
        failures = [
            StorageUnavailable("database offline"),
            CommitOutcomeUnknown("unknown COMMIT"),
            RpcUnavailable("provider omitted evidence"),
            RpcProtocolError("malformed header"),
            OSError("prior writer retirement unconfirmed"),
        ]
        for failure in failures:
            with self.subTest(cause=type(failure).__name__):
                with (
                    patch.object(supervisor, "supervise", AsyncMock(side_effect=failure)),
                    patch.object(reorg, "recover_reorganization", AsyncMock()) as recover,
                ):
                    with self.assertRaises(type(failure)) as caught:
                        await reorg.supervise_with_reorg_recovery(
                            object(), object(), asyncio.Event(),
                        )
                self.assertIs(caught.exception, failure)
                recover.assert_not_awaited()

    async def test_stop_during_recovery_preserves_original_failure_and_no_restart(self):
        stop = asyncio.Event()
        error = BranchConflict("fork")
        calls = []

        async def run(*args):
            calls.append(True)
            raise error

        async def recover(*args, **kwargs):
            stop.set()
            return True

        with (
            patch.object(supervisor, "supervise", run),
            patch.object(reorg, "recover_reorganization", recover),
        ):
            with self.assertRaises(BranchConflict) as caught:
                await reorg.supervise_with_reorg_recovery(object(), object(), stop)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(calls), 1)

    async def test_cancel_during_recovery_never_restarts(self):
        entered = asyncio.Event()
        calls = []

        async def run(*args):
            calls.append(True)
            raise BranchConflict("fork")

        async def recover(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with (
            patch.object(supervisor, "supervise", run),
            patch.object(reorg, "recover_reorganization", recover),
        ):
            task = asyncio.create_task(
                reorg.supervise_with_reorg_recovery(object(), object(), asyncio.Event())
            )
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(calls), 1)


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires owner-initialized disposable Anvil and PostgreSQL",
)
class RealReorganizationRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = os.environ["CONIX_TEST_ANVIL_URL"]
        self.dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        self.reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]
        self.http = httpx.AsyncClient(trust_env=False, timeout=5)

        genesis = await self.rpc("eth_getBlockByNumber", ["0x0", False])
        self.catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=self.url)
        self.token = "0x" + "11" * 20
        await self.rpc("evm_mine", [])
        async with open_writer(
            catalog=self.catalog, database_dsn=self.dsn, rpc_url=self.url,
        ) as writer:
            while await writer.advance_one() is not None:
                pass

    async def asyncTearDown(self):
        await self.http.aclose()

    async def rpc(self, method, params):
        response = await self.http.post(
            self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        document = response.json()
        self.assertNotIn("error", document)
        return document["result"]

    async def progress(self):
        async with await AsyncConnection.connect(self.reader_dsn, connect_timeout=5) as sql:
            return await (await sql.execute(
                "SELECT last_block, revision, generation, blocked_reason "
                "FROM conix.source_progress WHERE singleton = 1"
            )).fetchone()

    async def catch_up(self):
        async with open_writer(
            catalog=self.catalog, database_dsn=self.dsn, rpc_url=self.url,
        ) as writer:
            while await writer.advance_one() is not None:
                pass
            return await writer.journal.inspect()

    async def supervised_until(self, target, previous_generation):
        stop = asyncio.Event()
        application = asyncio.create_task(
            reorg.supervise_with_reorg_recovery(self.catalog, self.dsn, stop)
        )
        try:
            async with asyncio.timeout(25):
                while True:
                    if application.done():
                        await application
                        self.fail("Recovery application stopped before reaching replacement head.")
                    state = await self.progress()
                    if state[0] == target and state[2] != previous_generation:
                        return state
                    await asyncio.sleep(0.05)
        finally:
            stop.set()
            await asyncio.wait_for(application, timeout=10)

    async def test_real_multiblock_reorg_reindexes_replacement_event_and_invalidates_reader(self):
        account = (await self.rpc("eth_accounts", []))[0]

        def transfer_runtime(value):
            return (
                b"\x60" + bytes([value]) + b"\x60\x00\x52"
                + b"".join(b"\x7f" + topic for topic in reversed(ERC20.topics))
                + b"\x60\x20\x60\x00\xa3\x00"
            )

        await self.rpc("anvil_setCode", [self.token, "0x" + transfer_runtime(7).hex()])
        snapshot = await self.rpc("evm_snapshot", [])
        original_hash = await self.rpc("eth_sendTransaction", [{
            "from": account, "to": self.token, "gas": "0x100000",
        }])
        for _ in range(2):
            await self.rpc("evm_mine", [])
        old = await self.catch_up()

        async with open_reader(
            catalog=self.catalog, database_dsn=self.reader_dsn, rpc_url=self.url,
        ) as reader:
            old_checkpoint = await reader.checkpoint()
            old_page = await reader.events(
                checkpoint=old_checkpoint, contract="token",
                event=TRANSFER_SIGNATURE,
            )
            self.assertIn(
                bytes.fromhex(original_hash[2:]),
                {event.transaction_hash for event in old_page.events},
            )

        self.assertTrue(await self.rpc("evm_revert", [snapshot]))
        await self.rpc("evm_increaseTime", [60])
        await self.rpc("anvil_setCode", [self.token, "0x" + transfer_runtime(9).hex()])
        new_hash = await self.rpc("eth_sendTransaction", [{
            "from": account, "to": self.token, "gas": "0x100000",
            "data": "0x09",
        }])
        for _ in range(2):
            await self.rpc("evm_mine", [])
        self.assertNotEqual(original_hash, new_hash)
        replacement_head = int((
            await self.rpc("eth_getBlockByNumber", ["latest", False])
        )["number"], 16)
        self.assertEqual(replacement_head, old.last_block.number)

        before = await self.progress()
        self.assertEqual(before[2], old.generation)

        # The first ordinary T1-B session fails on the original retained tip.
        # T1-C then uses only the original checked repair and starts a fresh
        # instance of T1-B, whose app.run publishes the replacement.
        original_application = app.run
        original_journal_repair = Journal.repair_to
        original_writer = open_writer
        applications = []
        repaired_ancestors = []
        retired = []
        active = 0

        async def real_application(*args, **kwargs):
            applications.append(args)
            return await original_application(*args, **kwargs)

        async def real_repair(journal, *, ancestor):
            result = await original_journal_repair(journal, ancestor=ancestor)
            repaired_ancestors.append(ancestor)
            return result

        @asynccontextmanager
        async def observed_writer(**kwargs):
            nonlocal active
            selected = None
            try:
                async with original_writer(**kwargs) as writer:
                    self.assertEqual(active, 0, "Recovery admitted a second live writer.")
                    active += 1
                    selected = writer
                    yield writer
            finally:
                if selected is not None:
                    self.assertTrue(
                        selected.journal.connection.closed,
                        "Recovery admitted work before the previous writer retired.",
                    )
                    active -= 1
                    retired.append(selected)

        with (
            patch.object(rpc_retry.app, "run", real_application),
            patch.object(Journal, "repair_to", real_repair),
            patch.object(rpc_retry, "open_writer", observed_writer),
            patch.object(reorg, "open_writer", observed_writer),
        ):
            updated = await self.supervised_until(replacement_head, old.generation)

        self.assertEqual(active, 0)
        self.assertGreaterEqual(len(retired), 3)
        self.assertGreaterEqual(len(applications), 2)
        self.assertEqual(len(repaired_ancestors), 1)
        self.assertIsNone(updated[3])
        self.assertGreater(updated[1], old.revision)

        async with open_reader(
            catalog=self.catalog, database_dsn=self.reader_dsn, rpc_url=self.url,
        ) as reader:
            current = await reader.checkpoint()
            self.assertEqual(current.block.number, replacement_head)
            self.assertNotEqual(current.generation, old_checkpoint.generation)
            with self.assertRaises(CursorInvalid):
                await reader.events(
                    checkpoint=old_checkpoint, contract="token",
                    event=TRANSFER_SIGNATURE,
                )
            page = await reader.events(
                checkpoint=current, contract="token",
                event=TRANSFER_SIGNATURE,
            )
            self.assertTrue(page.events)
            self.assertEqual(page.events[-1].data, b"\x00" * 31 + b"\x09")
            self.assertEqual(page.events[-1].transaction_hash, bytes.fromhex(new_hash[2:]))

        async with await AsyncConnection.connect(self.reader_dsn, connect_timeout=5) as sql:
            replaced = await (await sql.execute(
                "SELECT count(*) FROM conix.events WHERE transaction_hash = %s",
                (bytes.fromhex(original_hash[2:]),),
            )).fetchone()
        self.assertEqual(replaced, (0,))

    async def test_real_persisted_blocked_history_recovers_after_fresh_start(self):
        snapshot = await self.rpc("evm_snapshot", [])
        for _ in range(2):
            await self.rpc("evm_mine", [])
        before = await self.catch_up()
        self.assertTrue(await self.rpc("evm_revert", [snapshot]))
        await self.rpc("evm_increaseTime", [60])
        for _ in range(2):
            await self.rpc("evm_mine", [])

        # Simulate a persisted intervention-required reorg marker from an
        # earlier process; T1-C must not depend on its in-memory exception.
        async with await AsyncConnection.connect(self.dsn, connect_timeout=5) as sql:
            await sql.execute(
                "UPDATE conix.source_progress SET blocked_reason = %s WHERE singleton = 1",
                (REORGANIZATION_BLOCKED_REASON,),
            )

        recovered = await self.supervised_until(before.last_block.number, before.generation)
        self.assertIsNone(recovered[3])
        self.assertNotEqual(recovered[2], before.generation)


if __name__ == "__main__":
    unittest.main()
