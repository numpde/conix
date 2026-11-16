"""Offline exact-RPC and independent-head contracts for Stage C."""

from contextlib import contextmanager
from http.server import HTTPServer
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import asyncio

import httpx

from stress_archive import rpc_key
import stress_replay as replay
import stress_replay_probe as probe
from host_diagnostics import extract, admitted


def specimen(*, uppercase_hashes=False):
    """Small immutable matrix; no surrogate scanner, SQL or provider transport."""
    start = 100
    hashes = {n: "0x" + "ab" * 31 + format(n, "02x") for n in (0, 99, 100, 101, 102)}
    if uppercase_hashes:
        hashes = {n: "0x" + value[2:].upper() for n, value in hashes.items()}
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
        put("eth_getLogs", [{"blockHash": hashes[number].lower(), "address": address,
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
    def test_provider_hash_spelling_does_not_change_original_writer_query_keys(self):
        evidence, hashes, address, topic = specimen(uppercase_hashes=True)
        params = [{"blockHash": hashes[100].lower(), "address": address, "topics": [topic]}]
        self.assertEqual(evidence.response("eth_getLogs", params, 100), [])
        self.assertEqual(evidence.response("eth_getBlockByNumber", ["latest", False], 100)["hash"], hashes[100])

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

    def test_pre_release_rpc_is_observable_and_cannot_be_hidden_as_startup(self):
        before = self.server.status()["pre_release_requests"]
        response = self.post({"jsonrpc": "2.0", "id": 55, "method": "eth_chainId", "params": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.status()["pre_release_requests"], before + 1)

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


class CatchupDeadlineContracts(unittest.TestCase):
    def test_late_final_checkpoint_cannot_report_completed_catchup(self):
        """Final progress observed after expiry cannot authorize a success report."""
        catalog = SimpleNamespace(source_id=b"c" * 32,
                                  chain=SimpleNamespace(rpc_url="http://replay:8080", start_block=100))
        evidence = SimpleNamespace(catalog=catalog, start=100, final=102,
                                   timeline=[(0, 100), (1000000000, 102)], window_ns=1000000000)
        def state(elapsed, head, released=True):
            return {"released": released, "elapsed_ns": elapsed, "head": head,
                    "frozen": elapsed >= 1000000000, "errors": 0}
        states = [state(0, 100, False), state(0, 100),
                  state(100000000, 100), state(100000000, 100),
                  state(2100000000, 102), state(2100000000, 102), state(2100000000, 102)]
        checkpoints = [{"height": height, "revision": 1, "source_id": catalog.source_id.hex(), "blocked": None}
                       for height in (100, 102)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            credential = folder / "reader.dsn"
            credential.write_text("disposable fixture")
            with patch.object(probe, "ReplayEvidence", return_value=evidence), \
                 patch.object(probe, "prepare_catalog", return_value=catalog), \
                 patch.object(probe, "CREDENTIAL", credential), patch.object(probe, "RESULTS", folder), \
                 patch.object(probe, "service", side_effect=states), \
                 patch.object(probe, "progress", side_effect=checkpoints), \
                 patch.object(probe.time, "sleep"), \
                 patch.object(probe, "verify_reader", new_callable=AsyncMock, return_value={"events": 0}) as reader:
                probe.observe(cadence_ms=100, catchup_seconds=1, start_wait_seconds=10)
            result = json.loads((folder / "result.json").read_bytes())
            self.assertEqual(result["status"], "incomplete_catchup")
            self.assertIsNone(result["reader"])
            reader.assert_not_awaited()

    def test_reader_verification_must_finish_inside_the_remaining_grant(self):
        """A timely checkpoint alone does not qualify late Reader verification."""
        async def slow_reader(*args):
            await asyncio.sleep(0.05)
            return {"events": 0}
        with patch.object(probe, "verify_reader", side_effect=slow_reader):
            with self.assertRaises(TimeoutError):
                asyncio.run(probe.verify_before_deadline(None, None, None, remaining_seconds=0.001))


class ReplayDiagnosticContracts(unittest.TestCase):
    def test_real_entrypoints_preserve_startup_failure_type_and_location(self):
        """An unavailable input must cross the real child boundary with its cause."""
        for module, arguments, phase in (
            (replay, ["serve"], "verification"),
            (probe, ["--sample-ms", "1000", "--catchup-seconds", "10", "--start-wait-seconds", "10"], "reader_check"),
        ):
            with self.subTest(entrypoint=module.__name__):
                result = subprocess.run([sys.executable, "-E", "-s", module.__file__, *arguments],
                                        capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                fields = admitted(extract(result.stderr))
                self.assertIsNotNone(fields)
                self.assertEqual(fields["cause"], "FileNotFoundError")
                self.assertEqual(fields["phase"], phase)
                self.assertNotEqual(fields["location"], "unavailable")


if __name__ == "__main__":
    unittest.main()
