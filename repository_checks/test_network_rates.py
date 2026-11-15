"""The network sampler has public read authority, never fixture-update authority."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.containers.client import OperationError
from tools.fixtures import network_rates


class NetworkRateAdmissionContracts(unittest.TestCase):
    def observation(self):
        source = {"sampling": {"height_positions_percent": [100], "contiguous_tail_blocks": 3, "window_span_blocks": 8, "response_limit_bytes": 4194304},
                  "selector_probe": {"address": "0x" + "00" * 20, "topic0": "0x" + "11" * 32},
                  "networks": [{"id": "sample", "chain_id": 1, "rpc_url": "https://public.invalid", "retired": False}]}
        raw_source = json.dumps(source).encode()

        def header(height):
            return {"height": height, "hash": "0x" + f"{height:064x}", "parent_hash": "0x" + f"{max(0, height-1):064x}",
                    "timestamp": 1700000000 + height, "transactions": 0, "response_bytes": 500, "elapsed_ms": 10}

        first, last = header(2), header(10)
        window = {"id": "head", "status": "observed", "first": first, "last": last,
                  "tail": [header(i) for i in (8, 9, 10)], "height_span": 8, "duration_ms": 8000,
                  "timestamp_resolution_ms": 1000, "mean_numbered_block_interval_ms": 1000,
                  "selector_probe": {"status": "observed", "log_count": 0, "elapsed_ms": 10, "response_bytes": 30,
                                     "filter": {"blockHash": last["hash"], "address": source["selector_probe"]["address"],
                                                "topics": [source["selector_probe"]["topic0"]]}}}
        stamp = datetime.now(timezone.utc).isoformat()
        result = {"kind": "observed_network_rates", "source_sha256": hashlib.sha256(raw_source).hexdigest(),
                  "started_at_utc": stamp, "completed_at_utc": stamp,
                  "networks": [{"id": "sample", "chain_id": 1, "endpoint": "https://public.invalid", "phase": "completed", "identity_assurance": "chain_id_checked_genesis_observed", "status": "observed",
                                "head": last, "genesis": {"status": "observed", "header": header(0)}, "windows": [window]}]}
        return result, raw_source

    def test_source_bound_population_and_empty_selector_measurement(self):
        result, source = self.observation()
        self.assertEqual(network_rates.admit_observations(json.dumps(result).encode(), source), result)
        mutations = [
            lambda d: d.update(source_sha256="0" * 64),
            lambda d: d.update(networks=[]),
            lambda d: d["networks"].append(deepcopy(d["networks"][0])),
            lambda d: d["networks"][0].update(chain_id=2),
            lambda d: d["networks"][0].update(status="retired"),
            lambda d: d["networks"][0].update(status="unavailable"),
            lambda d: d["networks"][0].update(head={**d["networks"][0]["head"], "height": 20}),
            lambda d: d["networks"][0]["windows"][0]["tail"][1].update(parent_hash="0x" + "aa" * 32),
            lambda d: d["networks"][0]["windows"][0]["tail"][1].update(height=2),
            lambda d: d["networks"][0]["windows"][0].update(mean_numbered_block_interval_ms=0),
            lambda d: d["networks"][0]["windows"][0].update(duration_ms=7000, mean_numbered_block_interval_ms=875),
            lambda d: d["networks"][0]["windows"][0]["selector_probe"]["filter"].update(blockHash="0x" + "aa" * 32),
        ]
        for mutation in mutations:
            changed = deepcopy(result)
            mutation(changed)
            with self.assertRaises(OperationError):
                network_rates.admit_observations(json.dumps(changed).encode(), source)

    def test_subsecond_anchor_and_timestamp_fields_must_agree(self):
        report, source = self.observation()
        window = report["networks"][0]["windows"][0]
        for item in [window["first"], window["last"], *window["tail"]]:
            item.update(timestamp_ms=item["timestamp"] * 1000, timestamp_field="timestampMs")
        window["timestamp_resolution_ms"] = 1
        network_rates.admit_observations(json.dumps(report).encode(), source)
        window["tail"][-1]["timestamp_ms"] += 200
        with self.assertRaises(OperationError):
            network_rates.admit_observations(json.dumps(report).encode(), source)
        window["tail"][-1]["timestamp_ms"] -= 200
        window["first"]["timestamp_field"] = "timestampMilliseconds"
        with self.assertRaises(OperationError):
            network_rates.admit_observations(json.dumps(report).encode(), source)

    def test_actual_catalog_snapshot_covers_mainnets_testnets_and_retirement(self):
        source = json.loads(network_rates.SOURCES.read_text())
        self.assertEqual(len(source["networks"]), 30)
        self.assertEqual(sum(network["retired"] for network in source["networks"]), 1)
        self.assertEqual(source["sampling"]["height_positions_percent"], [100, 90, 50, 10])
        for network in source["networks"]:
            self.assertNotIn("{API_KEY}", network["rpc_url"] or "")
        keys = {network["id"] for network in source["networks"]}
        for name in ("ethereum-mainnet", "ethereum-sepolia", "base-mainnet", "base-sepolia", "avalanche-mainnet", "avalanche-fuji"):
            self.assertIn(name, keys)

    def test_committed_observations_are_source_bound_and_keep_coverage_gaps(self):
        folder = network_rates.SOURCES.parent
        observed = network_rates.admit_observations((folder / "network_rates.json").read_bytes(), network_rates.SOURCES.read_bytes())
        self.assertEqual(observed["collection_artifact"]["source_sha256"], observed["source_sha256"])
        self.assertRegex(observed["collection_artifact"]["runner_image"], r"^sha256:[0-9a-f]{64}$")
        references = json.loads((folder / "network_rate_references.json").read_text())
        self.assertEqual(references["kind"], "documented_rate_eras_not_measurements")
        for row in observed["networks"]:
            if row["identity_assurance"] == "chain_id_checked" and row["status"] in ("observed", "partial"):
                self.assertEqual(row["genesis"]["status"], "unavailable")
            for window in row["windows"]:
                if window["status"] == "unavailable":
                    self.assertNotIn("mean_numbered_block_interval_ms", window)
                elif window["selector_probe"]["status"] == "unavailable":
                    self.assertNotIn("elapsed_ms", window["selector_probe"])
        self.assertEqual(sum(row["status"] == "retired" for row in observed["networks"]), 1)

    def test_source_grant_precedes_egress_and_runtime_has_no_host_authority(self):
        self.run_collection(stale=True)
        self.run_collection(preflight_failure=True)
        self.run_collection(stale=False)

    def test_primary_survives_unconfirmed_owned_bridge_retirement(self):
        self.run_collection(cleanup_failure=True)

    def run_collection(self, *, stale=False, cleanup_failure=False, preflight_failure=False):
        from tools.fixtures.process import CampaignError
        report, source_bytes = self.observation()
        image = "sha256:" + "a" * 64
        primary = CampaignError("Primary collection failed.", exit_code=2)

        class Docker:
            endpoint = "unix:///selected"
            docker_command_prefix = ["docker", "--host", endpoint]
            docker_environment = {}
            def __init__(self):
                self.calls = []
                self.network = None
                self.owner = None
            def require_rootless_engine(self):
                pass
            def require_role(self, selected, role):
                assert (selected, role) == (image, "test-runner")
            def run(self, *arguments, **_kwargs):
                self.calls.append(arguments)
                if arguments[:2] == ("network", "create"):
                    self.network = arguments[-1]
                    self.owner = arguments[arguments.index("--label") + 1].split("=", 1)[1]
                if arguments[0] == "ps":
                    return ""
                if arguments[:2] == ("network", "ls"):
                    return self.network or ""
                if arguments[:2] == ("network", "inspect"):
                    return json.dumps([{"Name": self.network, "Labels": {network_rates.OWNER: "foreign" if cleanup_failure else self.owner}}])
                return ""

        docker = Docker()
        argv_seen = []
        def child(argv, **options):
            argv_seen.append(argv)
            if options["label"] == "Network-source grant verification":
                self.assertEqual(argv[argv.index("--network") + 1], "none")
                self.assertIsNone(docker.network)
                if preflight_failure:
                    options["error_file"].write_bytes(b'CONIX-FAILURE-V1 {"phase":"network_measure","cause":"FileNotFoundError","effect":"not_started","location":"unavailable"}\n')
                    raise primary
                return b"0" * 64 if stale else hashlib.sha256(source_bytes).hexdigest().encode()
            self.assertEqual(argv[argv.index("--network") + 1], docker.network)
            self.assertEqual(argv[argv.index("--user") + 1], "65532:65532")
            self.assertIn("--read-only", argv)
            self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
            for forbidden in ("--volume", "--mount", "--privileged", "--publish", "--env"):
                self.assertNotIn(forbidden, argv)
            if cleanup_failure:
                raise primary
            raw = json.dumps(report).encode()
            options["output"].write_bytes(raw)
            options["output"].chmod(0o600)
            return raw

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "sources.json").write_bytes(source_bytes)
            with patch.object(network_rates, "ROOT", folder), patch.object(network_rates, "SOURCES", folder / "sources.json"), patch.object(network_rates, "owned_command", side_effect=child), patch.object(network_rates, "progress"):
                if stale or cleanup_failure or preflight_failure:
                    with self.assertRaises(OperationError if stale or preflight_failure else CampaignError) as caught:
                        network_rates.collect(docker, image)
                    if cleanup_failure:
                        self.assertIs(caught.exception, primary)
                    if preflight_failure:
                        from tools.containers.diagnostics import admitted
                        self.assertEqual(admitted(caught.exception.diagnostic)["cause"], "FileNotFoundError")
                        self.assertIs(caught.exception.__cause__, primary)
                    self.assertFalse(list(folder.glob("out/network-rate-measurements/*/observations.json")))
                else:
                    network_rates.collect(docker, image)
                    self.assertEqual(len(list(folder.glob("out/network-rate-measurements/*/observations.json"))), 1)
            if stale or preflight_failure:
                self.assertFalse(any(call[:2] == ("network", "create") for call in docker.calls))
            if cleanup_failure:
                self.assertFalse(any(call[:2] == ("network", "rm") for call in docker.calls))
