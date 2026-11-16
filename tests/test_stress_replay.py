"""Offline exact-RPC and independent-head contracts for Stage C."""

from contextlib import contextmanager
from http.server import HTTPServer
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from stress_archive import rpc_key
import stress_replay as replay


def specimen():
    """Small immutable matrix; no surrogate scanner, SQL or provider transport."""
    start = 100
    hashes = {n: "0x" + format(n, "064x") for n in (0, 99, 100, 101, 102)}
    observed = []
    results = {}

    def put(method, params, result):
        results[rpc_key(method, params)] = result

    put("eth_chainId", [], "0xa869")
    for number, digest in hashes.items():
        put("eth_getBlockByNumber", [hex(number), False], {
            "number": hex(number), "hash": digest,
            "parentHash": hashes.get(number-1, "0x" + "0" * 64),
        })
    address = "0x" + "a" * 40
    topic = "0x" + "b" * 64
    for number in range(start, 103):
        put("eth_getLogs", [{"blockHash": hashes[number], "address": address,
                             "topics": [topic]}], [])
    # The live original process can legitimately request a numbered header
    # before that block exists. That historical null must not conflict with
    # Stage B's later admitted concrete header for the same immutable height.
    early_params = ["0x66", False]
    observed.append(SimpleNamespace(
        key=rpc_key("eth_getBlockByNumber", early_params),
        request={"id": 122},
        body=b'{"jsonrpc":"2.0","id":122,"result":null}',
        headers=(), result=lambda: None,
    ))
    for (method, params), value in results.items():
        reply = json.dumps({"jsonrpc": "2.0", "id": 123, "result": value,
                            "providerExtension": "unchanged"}, separators=(",", ":")).encode()
        observed.append(SimpleNamespace(
            key=(method, params), request={"id": 123}, body=reply,
            headers=(), result=lambda value=value: value,
        ))
    contract = SimpleNamespace(address=bytes.fromhex("a" * 40),
                               descriptor=SimpleNamespace(events=[
                                   SimpleNamespace(topic0=bytes.fromhex("b" * 64))]))
    catalog = SimpleNamespace(chain=SimpleNamespace(chain_id=43113, start_block=start,
                                                      rpc_url="https://source.example/rpc"),
                              contracts=[contract])
    polls = [
        {"header": {"height": 100}, "elapsed_start_ns": 5000,
         "elapsed_end_ns": 9000, "status": "observed"},
        {"header": {"height": 102}, "elapsed_start_ns": 13000,
         "elapsed_end_ns": 17000, "status": "observed"},
    ]
    manifest = {"observer": {"samples": polls}, "clocks": {"observer": {"uncertainty_ns": 20}}}
    with patch.object(replay, "admitted", return_value=(catalog, manifest, observed, polls, {})), \
         patch.object(replay, "_block", side_effect=lambda _raw, n: SimpleNamespace(
             header=SimpleNamespace(number=n))):
        model = replay.ReplayEvidence(Path("/no-files"))
    return model, hashes, address, topic


class HeadScheduleContracts(unittest.TestCase):
    def test_pre_release_is_frozen_and_head_advances_without_client_requests(self):
        evidence, _, _, _ = specimen()
        self.assertEqual(evidence.timeline, [(0, 100), (8000, 102)])
        now = [100]
        schedule = replay.HeadSchedule(evidence, monotonic=lambda: now[0])
        self.assertEqual(schedule.view()["head"], 100)
        self.assertFalse(schedule.view()["released"])
        now[0] += 1_000_000_000  # Delayed setup must not advance the schedule.
        self.assertEqual(schedule.view()["head"], 100)
        schedule.release()
        self.assertEqual(schedule.view()["elapsed_ns"], 0)
        now[0] += 7999
        self.assertEqual(schedule.view()["head"], 100)
        now[0] += 1
        self.assertEqual(schedule.view()["head"], 102)
        self.assertTrue(schedule.view()["frozen"])
        with self.assertRaises(ValueError):
            schedule.release()

    def test_id_adaptation_exact_selector_and_missing_data_are_fail_closed(self):
        evidence, hashes, address, topic = specimen()
        self.assertEqual(evidence.response("eth_chainId", [], 100), "0xa869")
        envelope = evidence.served_envelope("eth_chainId", [], 100, 54)
        self.assertEqual(envelope["id"], 54)
        self.assertEqual(envelope["providerExtension"], "unchanged")
        self.assertIsNone(evidence.response("eth_getBlockByNumber", ["0x66", False], 100))
        self.assertEqual(evidence.response("eth_getBlockByNumber", ["0x66", False], 102)["hash"], hashes[102])
        self.assertEqual(evidence.response("eth_getBlockByNumber", ["latest", False], 100)["hash"], hashes[100])
        self.assertEqual(evidence.response("eth_getBlockByNumber", ["0x63", False], 100)["hash"], hashes[99])
        self.assertEqual(evidence.response("eth_getLogs", [{"blockHash": hashes[100], "address": address,
                                                               "topics": [topic]}], 100), [])
        bad = [
            ("eth_getBlockByNumber", ["finalized", False]),
            ("eth_getBlockByNumber", ["0x64", True]),
            ("eth_getBlockByNumber", ["0x64", False, "extra"]),
            ("eth_getBlockByNumber", ["0x1", False]),  # No evidence below the predecessor.
            ("eth_getLogs", [{"fromBlock": "0x64", "toBlock": "latest", "address": address,
                              "topics": [topic]}]),
            ("eth_getLogs", [{"blockHash": hashes[100], "address": address.upper(),
                              "topics": [topic]}]),
            ("eth_getLogs", [{"blockHash": hashes[102], "address": address,
                              "topics": [topic]}]),  # Future immutable logs are not yet visible.
            ("eth_getCode", [address, "latest"]),
            ("eth_chainId", ["unexpected"]),
        ]
        for method, params in bad:
            with self.subTest(method=method, params=params), self.assertRaises(ValueError):
                evidence.response(method, params, 100)


class ReplayProtocolContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence, _, _, _ = specimen()
        cls.server = replay.ReplayHTTP(("127.0.0.1", 0), evidence, 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, obj):
        with httpx.Client(timeout=5, trust_env=False) as client:
            return client.post(self.url + "/", json=obj)

    def test_repeated_reads_preserve_payload_and_rebind_only_rpc_id(self):
        request = {"jsonrpc": "2.0", "id": 17, "method": "eth_getBlockByNumber",
                   "params": ["0x64", False]}
        first = self.post(request)
        second = self.post(dict(request, id=99))
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["id"], 17)
        self.assertEqual(second.json()["id"], 99)
        self.assertEqual(first.json()["result"], second.json()["result"])
        self.assertEqual(first.json()["providerExtension"], "unchanged")
        self.assertEqual(second.json()["providerExtension"], "unchanged")

    def test_invalid_requests_never_fall_back_or_become_empty_logs(self):
        for payload in (
            {"jsonrpc": "2.0", "id": True, "method": "eth_chainId", "params": []},
            {"jsonrpc": "2.0", "id": 4, "method": "eth_getLogs",
             "params": [{"fromBlock": "latest"}]},
            {"jsonrpc": "2.0", "id": 4, "method": "eth_getBlockByHash", "params": []},
        ):
            with self.subTest(payload=payload):
                response = self.post(payload)
                self.assertNotEqual(response.status_code, 200)
                self.assertNotIn("result", response.json())

    def test_delay_applies_to_repeated_immutable_and_latest_queries(self):
        self.server.delay_ms = 20
        try:
            count, elapsed = self.server.requests, self.server.wait_ns
            for method, params in (("eth_chainId", []), ("eth_chainId", []),
                                   ("eth_getBlockByNumber", ["latest", False]),
                                   ("eth_getBlockByNumber", ["0x66", False])):
                response = self.post({"jsonrpc": "2.0", "id": 4, "method": method,
                                      "params": params})
                self.assertEqual(response.status_code, 200)
            self.assertEqual(self.server.requests - count, 4)
            self.assertGreaterEqual(self.server.wait_ns - elapsed, 4 * 15_000_000)
        finally:
            self.server.delay_ms = 0


class BundleContracts(unittest.TestCase):
    def test_tar_extraction_rejects_missing_entries_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("../unexpected", "manifest.json"):
                with self.subTest(member=name):
                    data = io.BytesIO()
                    with tarfile.open(fileobj=data, mode="w") as archive:
                        raw = b"payload"
                        info = tarfile.TarInfo(name)
                        info.size = len(raw)
                        archive.addfile(info, io.BytesIO(raw))
                    data.seek(0)
                    with tempfile.TemporaryDirectory(dir=directory) as child:
                        with self.assertRaises(ValueError):
                            replay.unpack(data, Path(child))
                    self.assertFalse((Path(directory) / "unexpected").exists())


if __name__ == "__main__":
    unittest.main()
