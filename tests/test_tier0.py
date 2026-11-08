"""Behavioral contracts for the concrete Tier-0 package.

PostgreSQL tests use an explicitly selected disposable database (no implicit
host dependency). Source-only tests also run without PostgreSQL.
"""

import asyncio
import os
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from eth_utils import to_checksum_address
from psycopg import AsyncConnection

from conix.commands import _target
from conix.configuration import Settings, load_settings
from conix.engine import advance_one, run_session
from conix.errors import BranchConflict, ConfigurationError, WriterBusy
from conix.journal import Journal
from conix.models import (
    Block, Checkpoint, Collection, CollectionCursor, Deployment,
    HistoryUnavailable, Phase, Position, ObservationUnavailable,
)
from conix.observer import Observer


ADDR = to_checksum_address(bytes.fromhex("11" * 20))
CUSTOMER = to_checksum_address(bytes.fromhex("22" * 20))
PAYEE = to_checksum_address(bytes.fromhex("33" * 20))
KEY = to_checksum_address(bytes.fromhex("44" * 20))


def settings():
    return Settings(
        Deployment(31337, ADDR), 1, "http://127.0.0.1:8545",
        Path("/tmp/conix-test-dsn-file"), 0.05, 2, 1024 * 1024,
        2, 25, 128, 5,
    )


def block(number, letter, parent):
    return Block(number, bytes([letter]) * 32, parent, 1710000000 + number)


def event(header, index=0, amount=7):
    return Collection(
        header.number, header.hash, header.timestamp,
        bytes([18]) * 32, 0, index,
        CUSTOMER, 12, PAYEE, amount,
        amount, bytes([19]) * 32,
    )


def position():
    return Position(
        CUSTOMER, 12, PAYEE, 3600, 2, ADDR, PAYEE,
        93, 7, 0, 0, 93, KEY, 0, 0, 0, 0, 93, ADDR, Phase.OPERATING,
    )


class Contract:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def voucher_collected_events(self, header):
        self.calls.append(("logs", header.number))
        return self.records.get(header.number, ())

    async def position(self, customer, channel_id, header):
        self.calls.append(("position", header.number))
        if (customer, channel_id) != (CUSTOMER, 12):
            raise ValueError("Unexpected test position.")
        return ADDR, position()


class Chain:
    """Controlled source; exercises the actual engine and observer implementations."""

    def __init__(self, settings, blocks, records=None):
        self.settings = settings
        self.blocks = {header.number: header for header in blocks}
        # A real RPC source supplies the canonical predecessor of the first
        # configured indexed block, even though conix does not retain it.
        if 1 in self.blocks and 0 not in self.blocks:
            self.blocks[0] = block(0, 0, bytes(32))
        self.contract = Contract(records or {})
        self.canonicals = []
        self.identity_checks = 0

    async def assert_source(self):
        self.identity_checks += 1

    async def current_head(self):
        return self.blocks[max(self.blocks)]

    async def header(self, number):
        if number not in self.blocks:
            raise ObservationUnavailable(f"Missing test block {number}.")
        return self.blocks[number]

    async def canonical(self, header):
        self.canonicals.append(header.number)
        return self.blocks.get(header.number) == header

    async def finalized(self):
        return self.blocks[1]

    async def connect(self, lower, upper):
        for height in range(lower.number + 1, upper.number + 1):
            if self.blocks[height].parent_hash != self.blocks[height - 1].hash:
                raise ObservationUnavailable("Broken finality ancestry.")


class BasicContractTests(unittest.TestCase):
    def test_repair_selector_requires_full_identity(self):
        assert _target("12:0x" + "ab" * 32) == (12, bytes.fromhex("ab" * 32))
        for value in ("12", "-1:0x" + "aa" * 32, "01:0x" + "bb" * 32, "1:0x00"):
            with self.subTest(value=value), self.assertRaises(Exception):
                _target(value)

    def test_complete_configuration_without_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conix.toml"
            path.write_text(
                "[source]\nchain_id=31337\ncontract_address='" + ADDR + "'\n"
                "start_block=1\nrpc_url='http://127.0.0.1:8545'\n"
                "[database]\ndsn_file='/tmp/conix-test-dsn-file'\n"
                "[limits]\npoll_seconds=1.0\nrpc_timeout_seconds=2.0\n"
                "operation_timeout_seconds=5.0\nmax_response_bytes=1000\n"
                "max_connections=2\nmax_page_size=20\nmax_evidence_blocks=40\n",
                encoding="utf-8",
            )
            result = load_settings(path)
            self.assertEqual(result.deployment, Deployment(31337, ADDR))
            path.write_text(path.read_text() + "\n[unexpected]\nfoo=1\n")
            with self.assertRaises(ValueError):
                load_settings(path)


class OperationDeadlineContracts(unittest.IsolatedAsyncioTestCase):
    async def test_one_block_operation_has_an_internal_deadline(self):
        configured = replace(settings(), operation_timeout_seconds=0.05)
        retired = asyncio.Event()

        class WaitingSource:
            settings = configured
            async def assert_source(self):
                pass
            async def current_head(self):
                await asyncio.Event().wait()

        class Writer:
            async def basis(self):
                return SimpleNamespace(blocked_reason=None, checkpoint=None)

        class OwnedJournal:
            @asynccontextmanager
            async def writer(self):
                try:
                    yield Writer()
                finally:
                    retired.set()

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                run_session(OwnedJournal(), WaitingSource(), asyncio.Event()),
                timeout=0.6,
            )
        self.assertLess(time.monotonic() - started, 0.35)
        self.assertTrue(retired.is_set())

    async def test_guarded_observation_deadline_returns_unavailable(self):
        configured = replace(settings(), operation_timeout_seconds=0.05)

        class WaitingJournal:
            settings = configured
            async def basis(self):
                await asyncio.Event().wait()

        class Source:
            settings = configured

        observer = Observer(WaitingJournal(), Source())
        with self.assertRaises(ObservationUnavailable):
            await asyncio.wait_for(observer.latest_checkpoint(), timeout=0.6)


@unittest.skipUnless(os.getenv("CONIX_TEST_DSN"), "Set CONIX_TEST_DSN for isolated PostgreSQL integration")
class PostgreSQLContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dsn = os.environ["CONIX_TEST_DSN"]
        self.config = settings()
        self.journal = Journal(self.dsn, self.config)
        # The bootstrap owns schema creation. Runtime must not require database CREATE.
        async with self.journal.writer() as writer:
            await writer.init()
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as connection:
            await connection.execute("TRUNCATE conix.collections, conix.blocks, conix.source_progress")
        async with self.journal.writer() as writer:
            await writer.init()

    async def asyncTearDown(self):
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as connection:
            await connection.execute("TRUNCATE conix.collections, conix.blocks, conix.source_progress")

    async def test_single_block_real_publication_and_observation(self):
        first = block(1, 1, bytes(32))
        second = block(2, 2, first.hash)
        fake = Chain(self.config, (first, second), {1: (event(first, amount=2**80),)})
        async with self.journal.writer() as writer:
            one = await advance_one(writer, fake)
            two = await advance_one(writer, fake)
        self.assertTrue(one.changed and two.changed)
        self.assertEqual((one.block_number, two.block_number), (1, 2))
        self.assertEqual(fake.contract.calls, [("logs", 1), ("logs", 2)])
        checkpoint = await Observer(self.journal, fake).latest_checkpoint()
        self.assertEqual((checkpoint.block, checkpoint.collection_count), (second, 1))
        observation = await Observer(self.journal, fake).latest_position(CUSTOMER, 12)
        self.assertEqual(observation.position.voucher_key, KEY)
        self.assertEqual(observation.checkpoint.block.hash, second.hash)
        page = await Observer(self.journal, fake).voucher_collected_events(
            checkpoint, cursor=None, limit=5
        )
        self.assertTrue(page.complete)
        self.assertEqual(page.voucher_collected_events[0].amount, 2**80)
        evidence = await Observer(self.journal, fake).canonical_evidence(
            checkpoint, (1, 2), include_finalized=True
        )
        self.assertEqual(evidence.blocks, (first, second))
        self.assertEqual(evidence.finalized, first)
        # Restart is not a new generation; no duplicate effect is produced.
        async with self.journal.writer() as writer:
            result = await advance_one(writer, fake)
            self.assertFalse(result.changed)
        self.assertEqual((await self.journal.basis()).collection_count, 1)

    async def test_runtime_database_role_has_no_cluster_or_ledger_authority(self):
        # The disposable campaign provisions a separate bootstrap owner and a
        # named restricted runtime identity. Exercising all SQL as POSTGRES_USER
        # would mask illicit cross-schema access in the production package.
        import psycopg

        async with await AsyncConnection.connect(self.dsn, autocommit=True) as connection:
            cursor = await connection.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            self.assertEqual(tuple(await cursor.fetchone()), (False,) * 5)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                await connection.execute("SELECT * FROM ledger.operator_private")

    async def test_each_sql_connection_has_statement_and_lock_deadlines(self):
        async with self.journal._connection() as connection:
            statement = await (await connection.execute("SHOW statement_timeout")).fetchone()
            locking = await (await connection.execute("SHOW lock_timeout")).fetchone()
        self.assertNotEqual(statement[0], "0")
        self.assertNotEqual(locking[0], "0")

    async def test_one_writer_on_same_session_as_all_mutations(self):
        async with self.journal.writer() as first:
            await first.basis()
            with self.assertRaises(WriterBusy):
                async with self.journal.writer():
                    pass
        async with self.journal.writer() as writer:
            await writer.basis()

    async def test_repair_reuses_one_checked_core_mutation(self):
        first = block(1, 1, bytes(32))
        second = block(2, 2, first.hash)
        fake = Chain(self.config, (first, second), {1: (event(first),)})
        async with self.journal.writer() as writer:
            await advance_one(writer, fake)
            await advance_one(writer, fake)
        original = await Observer(self.journal, fake).latest_checkpoint()
        async with self.journal.writer() as writer:
            await writer.invalidate(await writer.basis(), "Canonical source replaced block 2.")
        with self.assertRaises((ObservationUnavailable, HistoryUnavailable)):
            await Observer(self.journal, fake).voucher_collected_events(
                original, cursor=None, limit=5
            )
        async with self.journal.writer() as writer:
            await writer.repair_to(await writer.basis(), first, fake)
        replacement = block(2, 5, first.hash)
        fake.blocks[2] = replacement
        async with self.journal.writer() as writer:
            await advance_one(writer, fake)
        new = await Observer(self.journal, fake).latest_checkpoint()
        self.assertNotEqual(new.generation, original.generation)
        self.assertEqual(new.block, replacement)
        self.assertEqual(new.collection_count, 1)

    async def test_unknown_commit_recovery_reads_durable_progress(self):
        first = block(1, 1, bytes(32))
        fake = Chain(self.config, (first,), {1: (event(first),)})
        async with self.journal.writer() as writer:
            await advance_one(writer, fake)
        # This witness does not simulate transport loss after COMMIT; it proves
        # the recovery rule after an already committed but caller-unknown result.
        async with self.journal.writer() as writer:
            prior = await writer.basis()
            self.assertEqual(prior.checkpoint, first)
            result = await advance_one(writer, fake)
            self.assertFalse(result.changed)
        self.assertEqual((await self.journal.basis()).collection_count, 1)

    async def test_retained_source_identity_is_not_reassigned(self):
        altered = Settings(
            Deployment(31338, ADDR), 1, self.config.rpc_url,
            self.config.database_dsn_file, 0.05, 2, 1024 * 1024, 2, 25, 128, 5,
        )
        with self.assertRaises(ConfigurationError):
            await Journal(self.dsn, altered).basis()
