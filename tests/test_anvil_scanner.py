"""Qualify selected-log acquisition from an actual disposable Anvil EVM chain.

Only this test invokes Anvil-specific state setup and transaction submission.
The production scanner uses chain reads and eth_getLogs exclusively. Real
PostgreSQL transaction/repair behavior has its own installed-package tests.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest

import httpx

from abi_golden_vectors import ERC20, word
from conix.catalog import prepare_catalog
from conix.core import Writer
from conix.rpc import RPC
from test_core_rpc import RecordingJournal


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTRACT = b"\x11" * 20


def hex_bytes(value: bytes) -> str:
    return "0x" + value.hex()


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL"),
    "Requires the confined, disposable Anvil service",
)
class AnvilScannerContracts(unittest.IsolatedAsyncioTestCase):

    async def test_real_evm_transfer_log_is_scanned_with_original_bytes(self):
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

            # This 20-byte test address has no deployed source contract.
            # Install fixed runtime EVM instructions that MSTORE one uint256
            # and LOG3 the independently supplied ERC-20 Transfer topics.
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

            with tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                (directory / "abi").mkdir()
                (directory / "abi" / "erc20.json").write_bytes(
                    (FIXTURES / "abi" / "erc20.json").read_bytes()
                )
                original = (FIXTURES / "catalog.toml").read_text(encoding="utf-8")
                original = original.replace(
                    'genesis_hash = "0x' + "00" * 32 + '"',
                    f'genesis_hash = "{genesis["hash"].lower()}"',
                )
                original = original.replace(
                    "start_block = 1",
                    f'start_block = {int(receipt["blockNumber"], 16)}',
                )
                original = original.replace(
                    'rpc_url = "http://anvil:8545"',
                    "rpc_url = " + json.dumps(url),
                )
                config = directory / "source.toml"
                config.write_text(original, encoding="utf-8")
                source = prepare_catalog(config)

            journal = RecordingJournal(source)
            writer = Writer(
                catalog=source,
                journal=journal,
                rpc=RPC(client=client, url=url, limits=source.limits),
            )
            await writer.verify_source()
            completed = await writer.advance_one()

            self.assertEqual(completed.last_block.hash, bytes.fromhex(receipt["blockHash"][2:]))
            self.assertEqual(len(journal.publications), 1)

            (block, events) = journal.publications[0]
            self.assertEqual(block.number, int(receipt["blockNumber"], 16))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].transaction_hash, bytes.fromhex(transaction_hash[2:]))
            self.assertEqual(events[0].log_index, int(recorded_log["logIndex"], 16))
            self.assertEqual(events[0].address, CONTRACT)
            self.assertEqual(events[0].topics, ERC20.topics)
            self.assertEqual(events[0].data, word(7))

            # No new block is mined by a read. The complete retained tip
            # remains valid, and a second call cannot invent a new block.
            self.assertIsNone(await writer.advance_one())
            self.assertEqual(len(journal.publications), 1)


if __name__ == "__main__":
    unittest.main()
