"""Selected-log acquisition through real scanner methods and hostile RPC replies.

The server is a bounded in-process JSON-RPC transport. It verifies exact
request identity and scanner behavior, not PostgreSQL publication; the
journal's atomic persistence is tested independently against PostgreSQL.
"""

from dataclasses import replace
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

import httpx

from abi_golden_vectors import ERC20, ERC721
from conix.abi import ResourceExceeded
from conix.catalog import prepare_catalog
from conix.core import BranchConflict, Writer
from conix.journal import Block, Progress
from conix.rpc import RPC, RpcProtocolError, RpcUnavailable


FIXTURES = Path(__file__).resolve().parent / "fixtures"
TX0 = b"\xaa" * 32
TX1 = b"\xbb" * 32
HASH0 = b"\x00" * 32
HASH1 = b"\x11" * 32
HASH2 = b"\x22" * 32
TOKEN = b"\x11" * 20
COLLECTIBLE = b"\x22" * 20


def hex_bytes(value):
    return "0x" + value.hex()


def json_response(payload):
    """Supply unconsumed wire bytes to exercise the real streaming reader."""
    return httpx.Response(
        200,
        stream=httpx.ByteStream(json.dumps(payload).encode("utf-8")),
    )


def rpc_block(number, hash_, parent_hash, transactions=()):
    return {
        "number": hex(number),
        "hash": hex_bytes(hash_),
        "parentHash": hex_bytes(parent_hash),
        "timestamp": hex(1700000000 + number),
        "transactions": [hex_bytes(item) for item in transactions],
    }


def rpc_log(vector, address, transaction_index, log_index, transaction_hash):
    return {
        "blockHash": hex_bytes(HASH1),
        "blockNumber": "0x1",
        "transactionHash": hex_bytes(transaction_hash),
        "transactionIndex": hex(transaction_index),
        "logIndex": hex(log_index),
        "address": hex_bytes(address),
        "topics": [hex_bytes(word) for word in vector.topics],
        "data": hex_bytes(vector.data),
        "removed": False,
    }


class Server:
    def __init__(self):
        self.requests = []
        self.chain_id = "0x7a69"
        self.blocks = {
            "0x0": rpc_block(0, HASH0, HASH0),
            "0x1": rpc_block(1, HASH1, HASH0, (TX0, TX1)),
        }
        self.log_replies = {
            hex_bytes(TOKEN): [rpc_log(ERC20, TOKEN, 0, 3, TX0)],
            hex_bytes(COLLECTIBLE): [rpc_log(ERC721, COLLECTIBLE, 1, 7, TX1)],
        }
        self.mutate = None
        self.reorg = False
        self.candidate_reads = 0

    def __call__(self, request):
        operation = json.loads(request.content)
        self.requests.append(operation)
        method = operation["method"]
        arguments = operation["params"]

        if method == "eth_chainId":
            result = self.chain_id
        elif method == "eth_getBlockByNumber":
            if arguments[0] == "latest":
                available = [
                    (int(number, 16), block)
                    for (number, block) in self.blocks.items()
                    if block is not None
                ]
                result = max(available)[1]
            else:
                result = self.blocks.get(arguments[0])
            if arguments[0] == "0x1":
                self.candidate_reads += 1
                if self.reorg and self.candidate_reads > 1:
                    result = rpc_block(1, HASH2, HASH0, (TX0, TX1))
        elif method == "eth_getLogs":
            (filter_,) = arguments
            result = (
                self.log_replies.get(filter_["address"], [])
                if filter_["blockHash"] == hex_bytes(HASH1)
                else []
            )
            if self.mutate is not None:
                result = self.mutate(filter_, result)
        else:
            raise AssertionError(f"Unexpected scanner RPC method: {method}")

        return json_response({
            "jsonrpc": "2.0",
            "id": operation["id"],
            "result": result,
        })


class RecordingJournal:
    """Record the exact value passed to the real journal method by the scanner."""

    def __init__(self, catalog):
        self.basis = Progress(
            source_id=catalog.source_id,
            generation=UUID("00000000-0000-0000-0000-000000000001"),
            history_start=catalog.chain.start_block,
            revision=0,
            last_block=None,
            blocked_reason=None,
        )
        self.publications = []

    async def publish_block(self, *, block, events):
        self.publications.append((block, events))
        self.basis = replace(
            self.basis,
            last_block=block,
            revision=self.basis.revision + 1,
        )
        return self.basis


class ScannerEvidenceContracts(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.source = prepare_catalog(FIXTURES / "catalog.toml")
        self.server = Server()
        self.journal = RecordingJournal(self.source)

    async def writer(self, catalog=None):
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.server),
            trust_env=False,
            timeout=None,
        )
        self.addAsyncCleanup(self.client.aclose)
        selected = catalog or self.source
        writer = Writer(
            catalog=selected,
            journal=self.journal,
            rpc=RPC(client=self.client, url="http://selected.invalid/", limits=selected.limits),
        )
        await writer.verify_source()
        return writer

    async def test_one_selected_block_is_hash_pinned_and_next_empty_block_is_recorded(self):
        writer = await self.writer()

        first = await writer.advance_one()
        self.assertEqual(first.last_block.number, 1)
        self.assertEqual(len(self.journal.publications), 1)
        (block, events) = self.journal.publications[0]
        self.assertEqual(block.hash, HASH1)
        self.assertEqual(events[0].topics, ERC20.topics)
        self.assertEqual(events[0].data, ERC20.data)

        log_requests = [
            request for request in self.server.requests
            if request["method"] == "eth_getLogs"
        ]
        self.assertEqual([request["params"] for request in log_requests], [[{
            "blockHash": hex_bytes(HASH1),
            "address": hex_bytes(TOKEN),
            "topics": [hex_bytes(ERC20.topics[0])],
        }]])
        self.assertIsNone(await writer.advance_one())
        self.assertEqual(len(self.journal.publications), 1)

        self.server.blocks["0x2"] = rpc_block(2, HASH2, HASH1)
        self.assertEqual((await writer.advance_one()).last_block.number, 2)
        self.assertEqual(self.journal.publications[1][1], ())
        self.assertEqual(len(self.server.requests[-2]["params"][0]), 3)

    async def test_erc20_erc721_same_topic0_are_decoded_by_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "abi").mkdir()
            for name in ("erc20", "erc721"):
                (root / "abi" / (name + ".json")).write_bytes(
                    (FIXTURES / "abi" / (name + ".json")).read_bytes()
                )
            config = root / "catalog.toml"
            config.write_text(
                (FIXTURES / "catalog.toml").read_text().replace(
                    "[database]",
                    '[contracts.collectible]\n'
                    f'address = "{hex_bytes(COLLECTIBLE)}"\n'
                    'abi_file = "abi/erc721.json"\n'
                    'events = ["Transfer(address,address,uint256)"]\n'
                    "[database]",
                )
            )
            catalog = prepare_catalog(config)

        self.journal = RecordingJournal(catalog)
        writer = await self.writer(catalog)

        await writer.advance_one()
        (block, events) = self.journal.publications[0]
        self.assertEqual([event.log_index for event in events], [3, 7])
        self.assertEqual([event.data for event in events], [ERC20.data, ERC721.data])
        requests = [
            request["params"][0] for request in self.server.requests
            if request["method"] == "eth_getLogs"
        ]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["topics"], requests[1]["topics"])
        self.assertNotEqual(requests[0]["address"], requests[1]["address"])

    async def test_incomplete_or_contradictory_provider_replies_publish_nothing(self):
        changes = {
            "rpc error": lambda filter_, rows: {"failure": True},
            "wrong address": lambda filter_, rows: [{**rows[0], "address": hex_bytes(COLLECTIBLE)}],
            "wrong block": lambda filter_, rows: [{**rows[0], "blockHash": hex_bytes(HASH2)}],
            "wrong transaction": lambda filter_, rows: [{**rows[0], "transactionHash": hex_bytes(TX1)}],
            "removed log": lambda filter_, rows: [{**rows[0], "removed": True}],
            "duplicate": lambda filter_, rows: rows + rows,
            "bad ABI": lambda filter_, rows: [{**rows[0], "data": "0x01"}],
            "wrong topic": lambda filter_, rows: [
                {**rows[0], "topics": [hex_bytes(HASH2), *rows[0]["topics"][1:]]}
            ],
        }
        for (reason, mutate) in changes.items():
            with self.subTest(reason=reason):
                self.server = Server()
                self.journal = RecordingJournal(self.source)
                self.server.mutate = mutate
                writer = await self.writer()
                with self.assertRaises((RpcProtocolError, ResourceExceeded)):
                    await writer.advance_one()
                self.assertEqual(self.journal.publications, [])

    async def test_missing_selection_and_provider_error_refuse_publication(self):
        self.server.mutate = lambda filter_, rows: None
        writer = await self.writer()
        with self.assertRaises(RpcProtocolError):
            await writer.advance_one()
        self.assertEqual(self.journal.publications, [])

        # A JSON-RPC error is an unavailable observation, not a successful [].
        class Rejection(Server):
            def __call__(self, request):
                operation = json.loads(request.content)
                if operation["method"] == "eth_getLogs":
                    self.requests.append(operation)
                    return json_response({
                        "jsonrpc": "2.0",
                        "id": operation["id"],
                        "error": {"code": -32000, "message": "secret upstream text"},
                    })
                return super().__call__(request)

        self.server = Rejection()
        self.journal = RecordingJournal(self.source)
        writer = await self.writer()
        with self.assertRaises(RpcUnavailable) as error:
            await writer.advance_one()
        self.assertNotIn("secret upstream text", str(error.exception))
        self.assertEqual(self.journal.publications, [])

    async def test_wrong_chain_and_same_chain_id_with_wrong_genesis_are_refused(self):
        before = self.journal.basis
        self.server.chain_id = "0x1"
        with self.assertRaises(BranchConflict):
            await self.writer()
        self.assertEqual([request["method"] for request in self.server.requests], ["eth_chainId"])

        self.server = Server()
        self.server.blocks["0x0"] = rpc_block(0, HASH2, HASH0)
        with self.assertRaises(BranchConflict):
            await self.writer()
        self.assertEqual([request["method"] for request in self.server.requests],
                         ["eth_chainId", "eth_getBlockByNumber"])
        self.assertEqual(self.server.requests[1]["params"], ["0x0", False])
        self.assertEqual(self.journal.basis, before)
        self.assertEqual(self.journal.publications, [])

    async def test_descending_distinct_logs_are_refused_without_sorting(self):
        self.server.log_replies[hex_bytes(TOKEN)] = [
            rpc_log(ERC20, TOKEN, 1, 7, TX1), rpc_log(ERC20, TOKEN, 0, 3, TX0),
        ]
        writer = await self.writer()
        before = self.journal.basis
        with self.assertRaises(RpcProtocolError):
            await writer.advance_one()
        self.assertEqual(self.journal.basis, before)
        self.assertEqual(self.journal.publications, [])

    async def test_truncated_json_is_not_a_complete_log_response(self):
        class TruncatedLogs(Server):
            def __call__(self, request):
                operation = json.loads(request.content)
                if operation["method"] != "eth_getLogs":
                    return super().__call__(request)
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": operation["id"],
                    "result": self.log_replies[hex_bytes(TOKEN)],
                }).encode("utf-8")
                return httpx.Response(200, stream=httpx.ByteStream(payload[:-1]))

        self.server = TruncatedLogs()
        writer = await self.writer()
        before = self.journal.basis
        with self.assertRaises(RpcProtocolError) as failure:
            await writer.advance_one()
        self.assertIsInstance(failure.exception.__cause__, json.JSONDecodeError)
        self.assertEqual(self.journal.basis, before)
        self.assertEqual(self.journal.publications, [])

    async def test_rpc_reply_correlates_id_and_never_decompresses_opaque_bytes(self):
        class Compressed(Server):
            def __call__(self, request):
                operation = json.loads(request.content)
                if operation["method"] != "eth_getLogs":
                    return super().__call__(request)

                self.requests.append(operation)
                self.accept_encoding = request.headers.get("accept-encoding")
                valid_reply = json.dumps({
                    "jsonrpc": "2.0",
                    "id": operation["id"],
                    "result": self.log_replies[hex_bytes(TOKEN)],
                }).encode("utf-8")
                return httpx.Response(
                    200,
                    stream=httpx.ByteStream(gzip.compress(valid_reply)),
                    headers={"Content-Encoding": "gzip"},
                )

        self.server = Compressed()
        writer = await self.writer()
        with self.assertRaises(RpcProtocolError):
            await writer.advance_one()

        self.assertEqual(self.server.accept_encoding, "identity")
        self.assertEqual(self.journal.publications, [])

        class WrongId(Server):
            def __call__(self, request):
                operation = json.loads(request.content)
                if operation["method"] != "eth_getLogs":
                    return super().__call__(request)

                self.requests.append(operation)
                return json_response({
                    "jsonrpc": "2.0",
                    "id": operation["id"] + 1,
                    "result": self.log_replies[hex_bytes(TOKEN)],
                })

        self.server = WrongId()
        self.journal = RecordingJournal(self.source)
        writer = await self.writer()
        with self.assertRaises(RpcProtocolError):
            await writer.advance_one()
        self.assertEqual(self.journal.publications, [])

    async def test_missing_history_below_live_head_is_not_an_empty_tip(self):
        self.server.blocks["0x1"] = None
        self.server.blocks["0x2"] = rpc_block(2, HASH2, HASH1)
        writer = await self.writer()

        with self.assertRaises(RpcUnavailable):
            await writer.advance_one()
        self.assertEqual(self.journal.publications, [])

    async def test_reorg_between_acquisition_and_commit_refuses_publication(self):
        self.server.reorg = True
        writer = await self.writer()
        with self.assertRaises(BranchConflict):
            await writer.advance_one()
        self.assertEqual(self.journal.publications, [])

    async def test_oversized_rpc_reply_refuses_publication(self):
        writer = await self.writer()
        writer.rpc.limits = replace(
            self.source.limits,
            max_response_bytes=200,
        )

        with self.assertRaises(ResourceExceeded):
            await writer.advance_one()
        self.assertEqual(self.journal.publications, [])


if __name__ == "__main__":
    unittest.main()
