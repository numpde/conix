"""Index a real Anvil EVM event through the installed PostgreSQL writer.

Anvil-specific state changes and transaction submission are test-only.
The production writer uses exactly the selected read-only RPC methods and
the same atomic journal validated by ordinary PostgreSQL tests.
"""

import os
import unittest

import httpx
from psycopg import AsyncConnection

from abi_golden_vectors import ERC20, word
from anvil_catalog import catalog_for_anvil
from conix.core import open_writer


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

    async def test_real_evm_transfer_is_stored_and_empty_block_can_be_repaired(self):
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

            genesis = await call("eth_getBlockByNumber", ["0x0", False])
            self.assertEqual(await call("eth_chainId", []), "0x7a69")

            # The fixed runtime stores uint256(7) at memory 0, then LOG3
            # with independent ERC-20 Transfer topic bytes. No signing
            # key or test-only Anvil operation enters the scanner.
            runtime = (
                b"\x60\x07\x60\x00\x52"
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
            receipt = await call("eth_getTransactionReceipt", [transaction_hash])
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

                # Checked manual suffix repair is the same concrete journal
                # operation used by the scanner. It invalidates old handles.
                repaired = await writer.repair_to(ancestor=complete.last_block)
                self.assertEqual(repaired.last_block, complete.last_block)
                self.assertNotEqual(repaired.generation, complete.generation)

                # The now-missing canonical block can be acquired again.
                restored = await writer.advance_one()
                self.assertEqual(restored.last_block.number, mined_number + 1)
                self.assertEqual(restored.generation, repaired.generation)
                self.assertIsNone(await writer.advance_one())


if __name__ == "__main__":
    unittest.main()
