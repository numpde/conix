"""Stress capture admission, timing and retirement are testable without Docker."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.containers.client import OperationError
from tools.fixtures.stress_capture import (
    OWNER_LABEL, StressCapture, selected_source, supplied,
)

ROOT = Path(__file__).resolve().parents[1]


def parameters(**changed):
    values = {
        "stress_duration_seconds": "120",
        "stress_poll_cadence_ms": "1000",
        "stress_max_gap_ms": "5000",
        "stress_clock_tolerance_ms": "1000",
        "stress_archive_bytes": "67108864",
        "stress_exchanges": "4096",
        "stress_start_wait_seconds": "900",
    }
    values.update(changed)
    return SimpleNamespace(**values)


def admitted_graph():
    fixture = StressCapture.__new__(StressCapture)
    fixture.project = "conix-stress-001122334455"
    fixture.owner = "001122334455"
    fixture.runner = "sha256:" + "a" * 64
    fixture.images = {"postgres": "postgres@sha256:" + "b" * 64}
    services = {}
    for name in ("postgres", "bootstrap", "archive-init", "gateway", "observer"):
        networks = {
            "postgres": {"tests"}, "bootstrap": {"tests"},
            "archive-init": set(), "gateway": {"rpc", "egress"},
            "observer": {"egress"},
        }[name]
        mounts = {
            "postgres": [("pgdata", "/var/lib/postgresql/data")],
            "bootstrap": [], "archive-init": [
                ("application", "/init/application"), ("observer", "/init/observer"),
            ],
            "gateway": [("application", "/capture")],
            "observer": [("observer", "/capture")],
        }[name]
        services[name] = {
            "image": fixture.images["postgres"] if name in ("postgres", "bootstrap") else fixture.runner,
            "pull_policy": "never", "read_only": True,
            "networks": {network: {} for network in networks},
            "labels": {OWNER_LABEL: fixture.owner}, "cap_drop": ["ALL"],
            "volumes": [{"type": "volume", "source": source, "target": target}
                        for (source, target) in mounts],
        }
    for name in ("gateway", "observer"):
        services[name].update(
            user="65532:65532", security_opt=["no-new-privileges:true"],
            mem_limit="256m", memswap_limit="256m", pids_limit=64,
            entrypoint=["python", "-E", "-s", "/tests/stress_" + name + ".py"],
        )
    services["archive-init"].update(
        user="0:0", cap_add=["CHOWN"], network_mode="none",
    )
    graph = {
        "services": services,
        "networks": {
            name: {"name": fixture.project + "_" + name,
                   "internal": name != "egress", "labels": {OWNER_LABEL: fixture.owner}}
            for name in ("tests", "rpc", "egress")
        },
        "volumes": {
            name: {"name": fixture.project + "_" + name,
                   "labels": {OWNER_LABEL: fixture.owner}}
            for name in ("pgdata", "application", "observer")
        },
    }
    return fixture, graph


class CaptureAdmissionContracts(unittest.TestCase):
    def test_maintained_lane_and_literal_variable_routing(self):
        result = subprocess.run(["make", "-s", "help"], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertIn("test/indexer/stress/capture", result.stdout)
        make = (ROOT / "Makefile").read_text()
        operator = (ROOT / "tools/containers/operator.py").read_text()
        for name in (
            "STRESS_DURATION_SECONDS", "STRESS_POLL_CADENCE_MS", "STRESS_MAX_GAP_MS",
            "STRESS_CLOCK_TOLERANCE_MS", "STRESS_ARCHIVE_BYTES", "STRESS_EXCHANGES",
            "STRESS_START_WAIT_SECONDS",
        ):
            self.assertIn("$(value " + name + ")", make)
            self.assertIn('"' + name + '"', operator)
        self.assertIn("from tools.fixtures.stress_capture import run_capture", operator)

    def test_selection_requires_complete_explicit_finite_budgets(self):
        chosen = supplied(parameters())
        self.assertEqual(chosen["stress_duration_seconds"], 120)
        for change in (
            {"stress_duration_seconds": ""}, {"stress_duration_seconds": "0"},
            {"stress_duration_seconds": "1; touch /tmp/escape"},
            {"stress_duration_seconds": "601"},
            {"stress_poll_cadence_ms": "0"},
            {"stress_exchanges": "10"},
            {"stress_max_gap_ms": "999"},
            {"stress_clock_tolerance_ms": "inf"},
            {"stress_archive_bytes": "0"},
            {"stress_start_wait_seconds": "901"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(OperationError):
                    supplied(parameters(**change))

    def test_public_source_matches_the_actual_17_event_abi(self):
        source, profile, raw, abi = selected_source()
        self.assertEqual(profile["chain"]["chain_id"], source["chain_id"])
        self.assertEqual(len(profile["contracts"]["payment"]["events"]), 17)
        self.assertTrue(raw and abi)

    def test_rendered_graph_refuses_unexpected_network_filesystem_and_privilege_grants(self):
        fixture, graph = admitted_graph()
        fixture.admit_graph(graph)
        changes = [
            lambda g: g["services"]["gateway"]["volumes"].append(
                {"type": "volume", "source": "pgdata", "target": "/database"}),
            lambda g: g["services"]["observer"]["networks"].update(rpc={}),
            lambda g: g["services"]["observer"].update(user="0:0"),
            lambda g: g["services"]["gateway"].update(mem_limit="512m"),
            lambda g: g["services"]["gateway"].update(entrypoint=["python", "/tests/fuji_rpc.py"]),
            lambda g: g["services"]["archive-init"]["cap_add"].append("SYS_ADMIN"),
            lambda g: g["networks"]["egress"].update(internal=True),
            lambda g: g["volumes"]["observer"]["labels"].update({OWNER_LABEL: "foreign"}),
        ]
        for change in changes:
            with self.subTest(change=change):
                altered = deepcopy(graph)
                change(altered)
                with self.assertRaises(OperationError):
                    fixture.admit_graph(altered)

    def test_clock_requires_two_consistent_bounded_brackets(self):
        fixture = StressCapture.__new__(StressCapture)
        fixture.selection = {"stress_clock_tolerance_ms": 100}
        before = {"lower_offset_ns": -10, "upper_offset_ns": 40}
        after = {"lower_offset_ns": 0, "upper_offset_ns": 20}
        self.assertEqual(fixture.clock_mapping(before, after)["offset_ns"], 10)
        with self.assertRaises(OperationError):
            fixture.clock_mapping(before, {"lower_offset_ns": 41, "upper_offset_ns": 50})

    def test_archive_cannot_be_exported_while_worker_is_not_retired(self):
        fixture = StressCapture.__new__(StressCapture)
        fixture.retired = set()
        fixture.docker = Mock()
        with self.assertRaises(OperationError):
            fixture.export("application")
        fixture.docker.assert_not_called()

    def test_engine_lifecycle_events_must_cover_one_uninterrupted_process(self):
        from datetime import datetime, timedelta, timezone

        fixture = StressCapture.__new__(StressCapture)
        fixture.docker = Mock()
        identity = "a" * 64
        started = datetime.now(timezone.utc)
        ended = started + timedelta(seconds=5)

        def feed(actions):
            fixture.docker.run.return_value = "\n".join(json.dumps({
                "Action": action, "Type": "container",
                "Actor": {"ID": identity}, "timeNano": (i + 1) * 1_000_000_000,
            }) for i, action in enumerate(actions))
        feed(["start"])
        result = fixture.history(identity, started, ended)
        self.assertEqual(result["events"][0]["action"], "start")
        for actions in ([], ["die"], ["start", "die"], ["start", "restart"], ["start", "start"]):
            with self.subTest(actions=actions):
                feed(actions)
                with self.assertRaises(OperationError):
                    fixture.history(identity, started, ended)

    def test_cleanup_retains_both_unexported_archive_volumes(self):
        fixture = StressCapture.__new__(StressCapture)
        fixture.fixture_attempted = True
        fixture.profile_created = False
        fixture.consumer_retired = True
        fixture.archive_exported = set()
        fixture.records = {}
        fixture.owner = "001122334455"
        fixture.project = "conix-stress-001122334455"
        fixture.docker = Mock()
        fixture.docker.run.return_value = ""
        fixture.verify_fixture_ownership = Mock()
        fixture.compose_run = Mock()
        fixture.owned_volume = Mock(side_effect=lambda name: fixture.project + "_" + name)

        self.assertEqual(fixture.cleanup(), [])
        fixture.docker.run.assert_any_call(
            "volume", "rm", fixture.project + "_pgdata", timeout=30,
        )
        fixture.owned_volume.assert_called_once_with("pgdata")
        fixture.compose_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
