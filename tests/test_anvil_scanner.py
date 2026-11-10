"""Index a real Anvil EVM event through the installed PostgreSQL writer.

Anvil-specific state changes and transaction submission are test-only.
The production writer uses exactly the selected read-only RPC methods and
the same atomic journal validated by ordinary PostgreSQL tests.
"""

import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
from psycopg import AsyncConnection, AsyncServerCursor

from abi_golden_vectors import ERC20, FROM, TO, TRANSFER_SIGNATURE, word
from anvil_catalog import catalog_for_anvil
from conix.core import BranchConflict, open_writer
from conix.reader import CursorInvalid, HistoricalCallUnavailable, HistoryUnavailable, open_reader
from conix.rpc import RpcUnavailable


CONTRACT = b"\x11" * 20


def hex_bytes(value: bytes) -> str:
    return "0x" + value.hex()


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL")
    and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires the disposable Anvil chain and its owner-initialized SQL database",
)
class AnvilScannerContracts(unittest.IsolatedAsyncioTestCase):

    async def test_real_evm_event_is_scanned_read_paginated_and_repaired(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]

        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async def call(method, params):
                response = await client.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params,
                    },
                )
                response.raise_for_status()
                document = response.json()
                self.assertEqual(document.get("jsonrpc"), "2.0")
                self.assertNotIn("error", document)
                return document["result"]

            async def mined_receipt(tx_hash):
                """Observe a mined transaction, not the submission acknowledgment."""
                deadline = asyncio.get_running_loop().time() + 5
                while True:
                    observed = await call("eth_getTransactionReceipt", [tx_hash])
                    if observed is not None:
                        return observed
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail("Anvil did not return the submitted transaction receipt.")
                    await asyncio.sleep(0.05)

            genesis = await call("eth_getBlockByNumber", ["0x0", False])
            self.assertEqual(await call("eth_chainId", []), "0x7a69")

            # Calldata-bearing view calls return uint256(42). Empty calldata
            # emits the original independent Transfer(LOG3) wire vector.
            # The fixture is test-only; the reader never submits a transaction.
            runtime = (
                b"\x36\x15\x60\x0f\x57"
                + b"\x60\x2a\x60\x00\x52\x60\x20\x60\x00\xf3"
                + b"\x5b\x60\x07\x60\x00\x52"
                + b"\x7f" + ERC20.topics[2]
                + b"\x7f" + ERC20.topics[1]
                + b"\x7f" + ERC20.topics[0]
                + b"\x60\x20\x60\x00\xa3\x00"
            )
            await call("anvil_setCode", [hex_bytes(CONTRACT), hex_bytes(runtime)])

            accounts = await call("eth_accounts", [])
            self.assertTrue(accounts)
            transaction_hash = await call(
                "eth_sendTransaction",
                [{
                    "from": accounts[0],
                    "to": hex_bytes(CONTRACT),
                    "gas": "0x100000",
                }],
            )
            receipt = await mined_receipt(transaction_hash)
            self.assertEqual(receipt["status"], "0x1")
            self.assertEqual(len(receipt["logs"]), 1)

            recorded_log = receipt["logs"][0]
            self.assertEqual(
                tuple(bytes.fromhex(topic[2:]) for topic in recorded_log["topics"]),
                ERC20.topics,
            )
            self.assertEqual(bytes.fromhex(recorded_log["data"][2:]), word(7))

            source = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            mined_number = int(receipt["blockNumber"], 16)

            async with open_writer(
                catalog=source,
                database_dsn=os.environ["CONIX_TEST_ANVIL_DSN"],
                rpc_url=url,
            ) as writer:
                for height in range(source.chain.start_block, mined_number + 1):
                    complete = await writer.advance_one()
                    self.assertIsNotNone(complete)
                    self.assertEqual(complete.last_block.number, height)

                self.assertEqual(
                    complete.last_block.hash,
                    bytes.fromhex(receipt["blockHash"][2:]),
                )
                self.assertIsNone(await writer.advance_one())

                async with open_reader(
                    catalog=source,
                    database_dsn=os.environ["CONIX_TEST_ANVIL_READ_DSN"],
                    rpc_url=url,
                ) as reader:
                    old_checkpoint = await reader.checkpoint()
                    self.assertEqual(old_checkpoint.block, complete.last_block)
                    selected = await reader.events(
                        checkpoint=old_checkpoint,
                        contract="token",
                        event=TRANSFER_SIGNATURE,
                        where={"to": TO},
                        limit=1,
                    )
                    self.assertEqual(len(selected.events), 1)
                    self.assertTrue(selected.complete)
                    self.assertIsNone(selected.next_cursor)
                    self.assertEqual(selected.events[0].values, (FROM, TO, 7))
                    self.assertEqual(selected.events[0].data, word(7))
                    self.assertEqual(selected.events[0].topics, ERC20.topics)

                    no_match = await reader.events(
                        checkpoint=old_checkpoint,
                        contract="token",
                        event=TRANSFER_SIGNATURE,
                        where={"to": "0x" + "33" * 20},
                    )
                    self.assertEqual(no_match.events, ())
                    self.assertTrue(no_match.complete)

                    # A historical ABI call uses the selected block hash,
                    # not the current tip or a latest-state fallback.
                    self.assertEqual(await reader.call(
                        checkpoint=old_checkpoint,
                        contract="token",
                        function="balanceOf(address)",
                        arguments=(FROM,),
                    ), (42,))

                    ancestry = await reader.evidence(
                        checkpoint=old_checkpoint,
                        block_numbers=(old_checkpoint.block.number,),
                        include_finalized=False,
                    )
                    self.assertEqual(ancestry.blocks, (old_checkpoint.block,))
                    self.assertIsNone(ancestry.finalized)
                    self.assertEqual(ancestry.finalized_status, "unknown")

                # A second actual block contains no selected Transfer.
                # It must still advance complete retained history.
                await call("evm_mine", [])
                empty = await writer.advance_one()
                self.assertEqual(empty.last_block.number, mined_number + 1)
                self.assertEqual(empty.generation, complete.generation)

                async with await AsyncConnection.connect(
                    os.environ["CONIX_TEST_ANVIL_READ_DSN"],
                    connect_timeout=5,
                ) as reader:
                    source_record = await (
                        await reader.execute(
                            "SELECT source_id, last_block, generation "
                            "FROM conix.source_progress WHERE singleton = 1"
                        )
                    ).fetchone()
                    self.assertEqual(
                        source_record,
                        (source.source_id, empty.last_block.number, empty.generation),
                    )
                    raw = await (
                        await reader.execute(
                            "SELECT transaction_hash, transaction_index, log_index, "
                            "address, topic0, topic1, topic2, topic3, data "
                            "FROM conix.events ORDER BY block_number, log_index"
                        )
                    ).fetchall()
                    self.assertEqual(raw, [(
                        bytes.fromhex(transaction_hash[2:]),
                        int(recorded_log["transactionIndex"], 16),
                        int(recorded_log["logIndex"], 16),
                        CONTRACT,
                        *ERC20.topics,
                        None,
                        word(7),
                    )])
                    blocks = await (
                        await reader.execute("SELECT number FROM conix.blocks ORDER BY number")
                    ).fetchall()
                    self.assertEqual(
                        [number for (number,) in blocks],
                        list(range(source.chain.start_block, mined_number + 2)),
                    )

                # A runtime DML credential cannot impersonate the dedicated
                # SELECT-only read API, despite having SELECT permissions.
                with self.assertRaises(HistoryUnavailable):
                    async with open_reader(
                        catalog=source,
                        database_dsn=os.environ["CONIX_TEST_ANVIL_DSN"],
                        rpc_url=url,
                    ):
                        self.fail("Runtime credentials were admitted as reader.")

                # Another real Transfer is mined after the empty block.
                # Paginating the new prefix must not change the old page.
                second_hash = await call("eth_sendTransaction", [{
                    "from": accounts[0],
                    "to": hex_bytes(CONTRACT),
                    "gas": "0x100000",
                }])
                second_receipt = await mined_receipt(second_hash)
                self.assertEqual(second_receipt["status"], "0x1")
                second_block = await writer.advance_one()
                self.assertEqual(second_block.last_block.number, mined_number + 2)

                async with open_reader(
                    catalog=source,
                    database_dsn=os.environ["CONIX_TEST_ANVIL_READ_DSN"],
                    rpc_url=url,
                ) as reader:
                    newest = await reader.checkpoint()
                    self.assertEqual(newest.block, second_block.last_block)
                    self.assertEqual(newest.generation, old_checkpoint.generation)

                    first_page = await reader.events(
                        checkpoint=newest,
                        contract="token",
                        event=TRANSFER_SIGNATURE,
                        where={"to": TO},
                        limit=1,
                    )
                    self.assertFalse(first_page.complete)
                    self.assertIsNotNone(first_page.next_cursor)

                    second_page = await reader.events(
                        checkpoint=newest,
                        contract="token",
                        event=TRANSFER_SIGNATURE,
                        where={"to": TO},
                        cursor=first_page.next_cursor,
                        limit=1,
                    )
                    self.assertTrue(second_page.complete)
                    self.assertIsNone(second_page.next_cursor)
                    self.assertEqual(len(second_page.events), 1)

                    # The row limit alone does not bound BYTEA page memory.
                    # A short byte budget should still return a resumable
                    # prefix rather than skip matching events.
                    with patch("conix.reader.MAX_PAGE_RAW_BYTES", 200):
                        byte_page = await reader.events(
                            checkpoint=newest,
                            contract="token",
                            event=TRANSFER_SIGNATURE,
                            where={"to": TO},
                            limit=100,
                        )
                        self.assertEqual(len(byte_page.events), 1)
                        self.assertFalse(byte_page.complete)
                        continued = await reader.events(
                            checkpoint=newest,
                            contract="token",
                            event=TRANSFER_SIGNATURE,
                            where={"to": TO},
                            cursor=byte_page.next_cursor,
                            limit=100,
                        )
                        self.assertEqual(len(continued.events), 1)
                        self.assertTrue(continued.complete)

                    self.assertEqual(
                        [event.transaction_hash for event in (
                            *first_page.events, *second_page.events,
                        )],
                        [bytes.fromhex(transaction_hash[2:]), bytes.fromhex(second_hash[2:])],
                    )

                    # A later append preserves the exact older complete prefix.
                    earlier = await reader.events(
                        checkpoint=old_checkpoint,
                        contract="token",
                        event=TRANSFER_SIGNATURE,
                        where={"to": TO},
                    )
                    self.assertEqual(len(earlier.events), 1)

                    with self.assertRaises(CursorInvalid):
                        await reader.events(
                            checkpoint=newest,
                            contract="token",
                            event=TRANSFER_SIGNATURE,
                            where={"from": FROM},
                            cursor=first_page.next_cursor,
                            limit=1,
                        )

                    ancestry = await reader.evidence(
                        checkpoint=newest,
                        block_numbers=(
                            old_checkpoint.block.number,
                            empty.last_block.number,
                            newest.block.number,
                        ),
                        include_finalized=False,
                    )
                    self.assertEqual(
                        [block.number for block in ancestry.blocks],
                        list(range(mined_number, mined_number + 3)),
                    )

                    # A provider can reorg immediately after a successful
                    # pinned eth_call. The reader must not return the result
                    # after observing a different canonical block header.
                    ordinary_rpc = reader.rpc.call

                    async def replaced_tip(method, params, *, deadline):
                        observed = await ordinary_rpc(method, params, deadline=deadline)
                        if (
                            method == "eth_getBlockByNumber"
                            and params[0] == hex(newest.block.number)
                        ):
                            return {**observed, "hash": "0x" + "ee" * 32}
                        return observed

                    with patch.object(reader.rpc, "call", side_effect=replaced_tip):
                        with self.assertRaises(BranchConflict):
                            await reader.call(
                                checkpoint=newest,
                                contract="token",
                                function="balanceOf(address)",
                                arguments=(FROM,),
                            )

                    # Finality means a PROVIDER report, not an independent
                    # proof. A missing finalized tag has explicit UNKNOWN
                    # status; a reported canonical header is KNOWN as reported.
                    rpc_call = reader.rpc.call

                    async def known_finality(method, params, *, deadline):
                        if method == "eth_getBlockByNumber" and params[0] == "finalized":
                            return await rpc_call(
                                method,
                                [hex(newest.block.number), False],
                                deadline=deadline,
                            )
                        return await rpc_call(method, params, deadline=deadline)

                    with patch.object(reader.rpc, "call", side_effect=known_finality):
                        reported = await reader.evidence(
                            checkpoint=newest,
                            block_numbers=(),
                            include_finalized=True,
                        )
                    self.assertEqual(reported.finalized_status, "known")
                    self.assertEqual(reported.finalized, newest.block)

                    async def inconsistent_finality(method, params, *, deadline):
                        if (
                            method == "eth_getBlockByNumber"
                            and params[0] in ("finalized", hex(old_checkpoint.block.number))
                        ):
                            older = await rpc_call(
                                method,
                                [hex(old_checkpoint.block.number), False],
                                deadline=deadline,
                            )
                            return {**older, "hash": "0x" + "ee" * 32}
                        return await rpc_call(method, params, deadline=deadline)

                    with patch.object(reader.rpc, "call", side_effect=inconsistent_finality):
                        with self.assertRaises(BranchConflict):
                            await reader.evidence(
                                checkpoint=newest,
                                block_numbers=(),
                                include_finalized=True,
                            )

                    async def missing_finality(method, params, *, deadline):
                        if method == "eth_getBlockByNumber" and params[0] == "finalized":
                            return None
                        return await rpc_call(method, params, deadline=deadline)

                    with patch.object(reader.rpc, "call", side_effect=missing_finality):
                        unavailable = await reader.evidence(
                            checkpoint=newest,
                            block_numbers=(),
                            include_finalized=True,
                        )
                    self.assertEqual(unavailable.finalized_status, "unknown")
                    self.assertIsNone(unavailable.finalized)

                    # Simulate an unsupported pinned eth_call. The reader
                    # must not retry against a number or latest block.
                    actual_rpc = reader.rpc.call
                    invoked = []

                    async def unsupported(method, params, *, deadline):
                        invoked.append((method, params))
                        if method == "eth_call":
                            raise RpcUnavailable("Provider refuses EIP-1898")
                        return await actual_rpc(method, params, deadline=deadline)

                    with patch.object(reader.rpc, "call", side_effect=unsupported):
                        with self.assertRaises(HistoricalCallUnavailable):
                            await reader.call(
                                checkpoint=newest,
                                contract="token",
                                function="balanceOf(address)",
                                arguments=(FROM,),
                            )
                    self.assertEqual([method for (method, _) in invoked], ["eth_getCode", "eth_call"])
                    self.assertEqual(
                        invoked[-1][1][-1],
                        {"blockHash": hex_bytes(newest.block.hash), "requireCanonical": True},
                    )

                    repaired = await self.repair_during_event_page(
                        reader, writer, newest, complete.last_block, first_page,
                    )

                    self.assertEqual(repaired.last_block, complete.last_block)
                    self.assertNotEqual(repaired.generation, complete.generation)

                    with self.assertRaises(CursorInvalid):
                        await reader.events(
                            checkpoint=old_checkpoint,
                            contract="token",
                            event=TRANSFER_SIGNATURE,
                        )
                    with self.assertRaises(CursorInvalid):
                        await reader.events(
                            checkpoint=newest,
                            contract="token",
                            event=TRANSFER_SIGNATURE,
                            cursor=first_page.next_cursor,
                        )

                    # A->B->A does not resurrect any pre-repair cursor.
                    restored_empty = await writer.advance_one()
                    self.assertEqual(restored_empty.last_block.number, mined_number + 1)
                    restored = await writer.advance_one()
                    self.assertEqual(restored.last_block.number, mined_number + 2)
                    self.assertEqual(restored.generation, repaired.generation)
                    self.assertIsNone(await writer.advance_one())

                    after_repair = await reader.checkpoint()
                    self.assertEqual(after_repair.generation, repaired.generation)
                    restored_page = await reader.events(
                        checkpoint=after_repair,
                        contract="token",
                        event=TRANSFER_SIGNATURE,
                        where={"to": TO},
                    )
                    self.assertEqual(len(restored_page.events), 2)
                    self.assertTrue(restored_page.complete)

    async def repair_during_event_page(self, reader, writer, checkpoint, ancestor, first_page):
        """Keep a real page snapshot open across an independent read and repair."""
        cursor_opened = asyncio.Event()
        repair_finished = asyncio.Event()
        execute = AsyncServerCursor.execute

        async def pause_page(cursor, *args, **kwargs):
            result = await execute(cursor, *args, **kwargs)
            if cursor.connection is reader.connection:
                cursor_opened.set()
                await repair_finished.wait()
            return result

        async def read_page(session):
            return await session.events(
                checkpoint=checkpoint, contract="token", event=TRANSFER_SIGNATURE,
                where={"to": TO}, limit=1,
            )

        async def expect_invalidated_page():
            with self.assertRaises(CursorInvalid):
                await read_page(reader)

        async with asyncio.timeout(10), open_reader(
            catalog=reader.catalog, database_dsn=os.environ["CONIX_TEST_ANVIL_READ_DSN"],
            rpc_url=os.environ["CONIX_TEST_ANVIL_URL"],
        ) as independent_reader:
            with patch.object(AsyncServerCursor, "execute", pause_page):
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(expect_invalidated_page())
                    await cursor_opened.wait()
                    self.assertEqual(await read_page(independent_reader), first_page)
                    repaired = await writer.repair_to(ancestor=ancestor)
                    repair_finished.set()
        return repaired


if __name__ == "__main__":
    unittest.main()
