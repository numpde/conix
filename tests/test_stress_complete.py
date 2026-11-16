"""Evidence completion covers the recorded interval without another indexer."""

from collections import Counter
import gzip
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from conix.rpc import RPC, RpcUnavailable
from http_recording import RecordingTransport
from stress_archive import Exchange, rpc_key
from stress_complete import ArchiveOutput, RecordedAsyncTransport, _timeline, collect, complete_matrix, verify_completion


def header(height):
    return {"number": hex(height), "hash": "0x" + height.to_bytes(32).hex(),
            "parentHash": "0x" + max(0, height - 1).to_bytes(32).hex(),
            "timestamp": hex(height), "transactions": []}


class Provider:
    def __init__(self, change=None):
        self.calls = Counter()
        self.change = change

    async def call(self, method, params, *, deadline):
        key = rpc_key(method, params)
        self.calls[key] += 1
        if method == "eth_chainId":
            result = "0xa869"
        elif method == "eth_getBlockByNumber":
            result = header(int(params[0], 16))
        elif method == "eth_getLogs":
            result = []
        else:
            raise AssertionError("Completion asked for an unrelated RPC operation.")
        return self.change(method, params, result, self.calls[key]) if self.change else result


class StressCompletionContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = SimpleNamespace(
            chain=SimpleNamespace(chain_id=43113, genesis_hash=bytes(32), start_block=2, rpc_url="https://example.com/rpc"),
            limits=SimpleNamespace(operation_timeout_seconds=10, rpc_timeout_seconds=2, max_response_bytes=2097152),
            contracts=(SimpleNamespace(address=b"\x11" * 20,
                                       descriptor=SimpleNamespace(events=(SimpleNamespace(topic0=b"\x22" * 32),))),),
        )
        self.polls = [{"status": "observed", "header": {
            "height": height, "hash": header(height)["hash"], "parent_hash": header(height)["parentHash"],
        }} for height in (2, 3, 4)]

    def exchange(self, method, params, result, identifier=1):
        request = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        body = json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}).encode()
        return Exchange(request, 200, body, (), 0, 1)

    async def run_matrix(self, provider, exchanges=()):
        return await complete_matrix(self.catalog, exchanges, self.polls, provider, time.monotonic() + 5)

    async def test_exact_matrix_reuses_captured_cells_and_rechecks_every_numbered_header(self):
        """Empty log arrays count only when captured for the exact hash and selector."""
        provider = Provider()
        selected = [{"blockHash": header(2)["hash"], "address": "0x" + "11" * 20,
                     "topics": ["0x" + "22" * 32]}]
        exchanges = [self.exchange("eth_getBlockByNumber", ["0x2", False], header(2), identifier)
                     for identifier in (7, 19)]
        exchanges.append(self.exchange("eth_getLogs", selected, []))
        receipt = await self.run_matrix(provider, exchanges)
        self.assertEqual((receipt["start_block"], receipt["final_block"], receipt["log_cells"]), (2, 4, 3))
        self.assertEqual(receipt["rechecks"], 5)  # genesis + predecessor + seed through final
        self.assertEqual(provider.calls[rpc_key("eth_getLogs", selected)], 0)
        self.assertEqual(provider.calls[rpc_key("eth_getBlockByNumber", ["0x2", False])], 1)

    async def test_conflicting_captured_duplicate_is_not_replaced_by_a_new_read(self):
        """A later provider response cannot repair contradictory retained evidence."""
        changed = dict(header(2), timestamp="0x9")
        provider = Provider()
        with self.assertRaisesRegex(ValueError, "conflict"):
            await self.run_matrix(provider, [self.exchange("eth_getBlockByNumber", ["0x2", False], raw)
                                             for raw in (header(2), changed)])
        self.assertEqual(provider.calls[rpc_key("eth_getBlockByNumber", ["0x2", False])], 0)

    async def test_observed_fork_conflict_and_non_array_logs_stop_admission(self):
        """Neither a changed branch nor unavailable logs can shrink the interval."""
        for alteration in ("branch", "logs"):
            def change(method, params, result, count):
                if alteration == "branch" and method == "eth_getBlockByNumber" and params[0] == "0x4":
                    return dict(result, hash="0x" + "ff" * 32)
                if alteration == "logs" and method == "eth_getLogs":
                    return None
                return result
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                await self.run_matrix(Provider(change))

    async def test_recheck_compares_full_block_results_including_transaction_inventory(self):
        """Matching header hashes alone cannot admit altered block evidence."""
        def change(method, params, result, count):
            if method == "eth_getBlockByNumber" and params[0] == "0x2" and count == 2:
                return dict(result, transactions=["0x" + "ab" * 32])
            return result
        with self.assertRaisesRegex(ValueError, "canonical recheck"):
            await self.run_matrix(Provider(change))

    async def test_original_rpc_decoder_consumes_recorded_gzip_without_double_decoding(self):
        """The adapter changes transport only; original RPC still validates IDs."""
        def reply(request):
            identifier = json.loads(request.content)["id"]
            raw = json.dumps({"jsonrpc": "2.0", "id": identifier, "result": []}).encode()
            return httpx.Response(200, content=gzip.compress(raw), headers={"Content-Encoding": "gzip"})
        recorder = RecordingTransport(httpx.MockTransport(reply), io.BytesIO(), max_request_bytes=16384,
                                      max_response_bytes=2097152, elapsed_budget_seconds=10,
                                      max_exchanges=4, max_archive_bytes=1048576)
        async with httpx.AsyncClient(transport=RecordedAsyncTransport(recorder)) as client:
            rpc = RPC(client=client, url="https://example.com/rpc", limits=self.catalog.limits)
            self.assertEqual(await rpc.call("eth_getLogs", [{}], deadline=time.monotonic() + 5), [])
        self.assertTrue(recorder.closed)
        self.assertEqual(recorder.exchanges, 1)

    async def test_request_timeout_joins_the_recording_thread_before_closing(self):
        """A bounded request failure survives synchronous transport retirement."""
        def reply(request):
            time.sleep(0.05)
            identifier = json.loads(request.content)["id"]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": identifier, "result": []})
        recorder = RecordingTransport(httpx.MockTransport(reply), io.BytesIO(), max_request_bytes=16384,
                                      max_response_bytes=2097152, elapsed_budget_seconds=10,
                                      max_exchanges=4, max_archive_bytes=1048576)
        async with httpx.AsyncClient(transport=RecordedAsyncTransport(recorder)) as client:
            rpc = RPC(client=client, url="https://example.com/rpc", limits=self.catalog.limits)
            with self.assertRaises(RpcUnavailable):
                await rpc.call("eth_getLogs", [{}], deadline=time.monotonic() + 0.01)
        self.assertTrue(recorder.closed)
        self.assertEqual(recorder.exchanges, 1)

    async def test_closed_completion_warc_rebuilds_the_whole_matrix_without_network(self):
        """Actual recorder and RPC machinery produce independently readable evidence."""
        def reply(request):
            selected = json.loads(request.content)
            (method, params) = selected["method"], selected["params"]
            if method == "eth_chainId":
                result = "0xa869"
            elif method == "eth_getBlockByNumber":
                result = header(int(params[0], 16))
            elif method == "eth_getLogs":
                result = []
            else:
                raise AssertionError("Unrelated RPC operation.")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": selected["id"], "result": result})
        output = io.BytesIO()
        with patch("stress_complete.upstream_addresses", autospec=True, return_value=["1.1.1.1"]), \
             patch("stress_complete.httpx.HTTPTransport", return_value=httpx.MockTransport(reply)):
            produced = await collect(self.catalog, [], self.polls, seconds=10, exchanges=128,
                                     archive_bytes=1048576, output=ArchiveOutput(output))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "completion.warc.gz"
            path.write_bytes(output.getvalue())
            verified = await verify_completion(folder, self.catalog, [], self.polls)
            self.assertEqual(verified["status"], "verified")
            self.assertEqual(verified["completion_exchanges"], produced["completion_exchanges"])
            for field in ("start_block", "final_block", "headers", "selectors", "log_cells", "rechecks"):
                self.assertEqual(verified["coverage"][field], produced[field])
            raw = gzip.decompress(path.read_bytes())
            path.write_bytes(raw[:raw.rfind(b"WARC/1.1\r\n")])
            with self.assertRaises(ValueError):
                await verify_completion(folder, self.catalog, [], self.polls)

    async def test_head_decrease_between_seed_and_first_timed_poll_is_refused(self):
        """The predecessor anchor must not conceal a real decrease after seeding."""
        polls = []
        exchanges = []
        for (identifier, height) in enumerate((3, 2, 4), 1):
            (start, end) = identifier * 10, identifier * 10 + 2
            poll = {"id": identifier, "status": "observed", "started_ns": start, "finished_ns": end,
                    "header": {"height": height, "hash": header(height)["hash"], "parent_hash": header(height)["parentHash"]}}
            polls.append(poll)
            exchange = self.exchange("eth_getBlockByNumber", ["latest", False], header(height), identifier)
            exchanges.append(Exchange(exchange.request, 200, exchange.body, (), start, end))
        point = {"sent": 100, "received": 200, "worker": 150, "lower_offset_ns": -50, "upper_offset_ns": 50}
        clock = {"before": point, "after": point, "offset_ns": 0, "uncertainty_ns": 50}
        manifest = {"capture": "complete", "process_continuity": "confirmed", "cleanup": [],
                    "observation_eligibility": {"status": "complete"}, "worker_exits": {"gateway": 0, "observer": 0},
                    "indexer_events": {"events": [{"action": "start", "time_ns": 1}]},
                    "selected_parameters": {"stress_clock_tolerance_ms": 1, "stress_max_gap_ms": 1},
                    "clocks": {"gateway": clock, "observer": clock}, "seed": polls[0], "start_block": 3,
                    "observer": {"seed": polls[0], "samples": polls[1:]}}
        with self.assertRaisesRegex(ValueError, "decrease"):
            _timeline(manifest, exchanges)


if __name__ == "__main__":
    unittest.main()
