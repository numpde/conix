"""T1-C1: select a retained canonical ancestor without altering indexed history.

The unit tests exercise the decision boundary. The contained Anvil test
uses original core RPC, the exclusive PostgreSQL journal and genuine forks.
"""

import asyncio
from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from psycopg import AsyncConnection

from anvil_catalog import catalog_for_anvil
from conix.core import BranchConflict, open_writer
from conix.journal import Block, HistoryConflict, Progress, RetainedAncestry
from conix.resilience.reorg import select_repair_ancestor
from conix.rpc import RpcUnavailable


def block(height):
    return Block(
        number=height,
        hash=bytes([height]) * 32,
        parent_hash=bytes([height - 1]) * 32,
        timestamp=1700000000 + height,
    )


class AncestorSelectionContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.blocks = tuple(block(height) for height in (9, 8, 7))
        self.basis = Progress(
            source_id=b"s" * 32,
            generation=uuid4(),
            history_start=1,
            revision=17,
            last_block=self.blocks[0],
            blocked_reason="reorganization requires repair",
        )
        self.seen = []
        self.journal = SimpleNamespace(
            retained_ancestry=AsyncMock(return_value=RetainedAncestry(
                progress=self.basis, blocks=self.blocks,
            )),
            inspect=AsyncMock(return_value=self.basis),
            repair_to=AsyncMock(side_effect=AssertionError("Selection must not repair.")),
        )

    def writer(self, selected):
        async def observed(number, deadline):
            self.seen.append((number, deadline))
            return None if selected[number] is None else SimpleNamespace(header=selected[number])

        return SimpleNamespace(
            journal=self.journal,
            catalog=SimpleNamespace(limits=SimpleNamespace(
                max_evidence_blocks=3,
                operation_timeout_seconds=5,
            )),
            _block=observed,
            repair_to=AsyncMock(side_effect=AssertionError("Selection must not repair.")),
        )

    async def test_highest_matching_ancestor_preserves_basis_and_never_repairs(self):
        changed = {9: replace(block(9), hash=b"x" * 32),
                   8: replace(block(8), hash=b"y" * 32),
                   7: block(7)}
        writer = self.writer(changed)
        selection = await select_repair_ancestor(writer)

        self.assertEqual(selection.ancestor, block(7))
        self.assertEqual(selection.basis, self.basis)
        self.assertEqual([number for number, _ in self.seen], [9, 8, 7])
        self.assertEqual(len({deadline for _, deadline in self.seen}), 1)
        self.journal.retained_ancestry.assert_awaited_once_with(limit=3)
        self.journal.inspect.assert_awaited_once()
        writer.repair_to.assert_not_awaited()
        self.journal.repair_to.assert_not_awaited()

    async def test_matching_tip_does_not_schedule_an_unnecessary_repair(self):
        writer = self.writer({9: block(9)})
        self.assertIsNone(await select_repair_ancestor(writer))
        self.assertEqual([number for number, _ in self.seen], [9])
        self.journal.inspect.assert_awaited_once()
        writer.repair_to.assert_not_awaited()

    async def test_bounded_search_cannot_invent_an_origin_rebuild(self):
        writer = self.writer({
            9: replace(block(9), hash=b"x" * 32),
            8: replace(block(8), hash=b"y" * 32),
            7: replace(block(7), hash=b"z" * 32),
        })
        with self.assertRaises(BranchConflict):
            await select_repair_ancestor(writer)
        self.assertEqual([number for number, _ in self.seen], [9, 8, 7])
        writer.repair_to.assert_not_awaited()

    async def test_missing_header_is_unavailable_not_a_confirmed_fork(self):
        writer = self.writer({9: None, 8: block(8), 7: block(7)})
        with self.assertRaises(RpcUnavailable):
            await select_repair_ancestor(writer)
        self.assertEqual([number for number, _ in self.seen], [9])

    async def test_durable_basis_change_refuses_a_stale_selection(self):
        self.journal.inspect.return_value = replace(self.basis, revision=18)
        writer = self.writer({
            9: replace(block(9), hash=b"x" * 32),
            8: block(8),
        })
        with self.assertRaises(HistoryConflict):
            await select_repair_ancestor(writer)
        self.assertEqual([number for number, _ in self.seen], [9, 8])

    async def test_empty_prefix_is_not_a_repair_target(self):
        self.journal.retained_ancestry.return_value = RetainedAncestry(
            progress=replace(self.basis, last_block=None), blocks=(),
        )
        writer = self.writer({})
        with self.assertRaises(HistoryConflict):
            await select_repair_ancestor(writer)
        self.assertEqual(self.seen, [])

    async def test_rpc_failure_and_cancellation_propagate(self):
        writer = self.writer({})

        async def fail(_number, _deadline):
            raise RpcUnavailable("Selected RPC source unavailable.")

        writer._block = fail
        with self.assertRaises(RpcUnavailable):
            await select_repair_ancestor(writer)

        async def cancelled(_number, _deadline):
            raise asyncio.CancelledError()

        writer._block = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await select_repair_ancestor(writer)
        writer.repair_to.assert_not_awaited()


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires the owner-initialized disposable Anvil and PostgreSQL",
)
class RealReorganizationSelectionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_genuine_multiblock_fork_selects_without_mutating_journal(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]

        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async def call(method, params):
                reply = await client.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                reply.raise_for_status()
                response = reply.json()
                self.assertNotIn("error", response)
                return response["result"]

            genesis = await call("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)

            # Establish an indexed prefix before snapshotting the test chain.
            await call("evm_mine", [])
            async with open_writer(catalog=catalog, database_dsn=dsn, rpc_url=url) as writer:
                while await writer.advance_one() is not None:
                    pass
                basis = await writer.journal.inspect()
            self.assertIsNotNone(basis.last_block)

            snapshot = await call("evm_snapshot", [])
            for _ in range(3):
                await call("evm_mine", [])

            async with open_writer(catalog=catalog, database_dsn=dsn, rpc_url=url) as writer:
                for _ in range(3):
                    self.assertIsNotNone(await writer.advance_one())
                old_tip = await writer.journal.inspect()
                self.assertIsNone(await select_repair_ancestor(writer))

            self.assertTrue(await call("evm_revert", [snapshot]))
            await call("evm_increaseTime", [60])
            for _ in range(3):
                await call("evm_mine", [])
            current = await call("eth_getBlockByNumber", [hex(old_tip.last_block.number), False])
            self.assertNotEqual(current["hash"], "0x" + old_tip.last_block.hash.hex())

            async with open_writer(catalog=catalog, database_dsn=dsn, rpc_url=url) as writer:
                with self.assertRaises(BranchConflict):
                    await writer.advance_one()
                selection = await select_repair_ancestor(writer)
                self.assertEqual(selection.ancestor, basis.last_block)
                self.assertEqual(selection.basis, old_tip)
                self.assertEqual(await writer.journal.inspect(), old_tip)

                async with await AsyncConnection.connect(reader_dsn, connect_timeout=5) as sql:
                    persisted = await (await sql.execute(
                        "SELECT last_block, generation, revision FROM conix.source_progress "
                        "WHERE singleton = 1"
                    )).fetchone()
                self.assertEqual(persisted, (
                    old_tip.last_block.number, old_tip.generation, old_tip.revision,
                ))

                # Fixture cleanup uses the ORIGINAL checked manual Tier-0 repair.
                # The T1-C1 selector itself has performed no mutation.
                repaired = await writer.repair_to(ancestor=selection.ancestor)
                self.assertNotEqual(repaired.generation, old_tip.generation)
                for _ in range(3):
                    self.assertIsNotNone(await writer.advance_one())
                self.assertIsNone(await writer.advance_one())


if __name__ == "__main__":
    unittest.main()
