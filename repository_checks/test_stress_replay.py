"""Stage C admission and resource-authority tests without Docker or provider access."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.containers.client import OperationError
from tools.fixtures.stress_replay import ReplayAttempt, admitted_bundle, comparison, supplied

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "a" * 64
POSTGRES = "postgres@sha256:" + "b" * 64


def options(**changes):
    values = {
        "capture_dir": "/capture", "evidence_dir": "/completion",
        "context": "agentic", "runner_image": IMAGE,
        "stress_replay_delay_ms": "250",
        "stress_checkpoint_cadence_ms": "1000",
        "stress_catchup_timeout_seconds": "240",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def graph_for(run):
    services = {}
    for name in ("postgres", "bootstrap", "volume-init", "replay"):
        image, networks, mounts = {
            "postgres": (POSTGRES, {"tests"}, [("pgdata", "/var/lib/postgresql/data")]),
            "bootstrap": (POSTGRES, {"tests"}, []),
            "volume-init": (IMAGE, set(), [("evidence", "/init/evidence"),
                                           ("results", "/init/results")]),
            "replay": (IMAGE, {"rpc"}, [("evidence", "/evidence")]),
        }[name]
        services[name] = {
            "image": image, "pull_policy": "never", "read_only": True,
            "networks": {network: {} for network in networks},
            "labels": {"io.conix.stress-replay-owner": run.owner},
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "volumes": [{"source": source, "target": dest, "type": "volume"}
                        for source, dest in mounts],
        }
    services["volume-init"].update(user="0:0", network_mode="none",
                                   cap_add=["CHOWN"])
    services["replay"].update(
        user="65532:65532", entrypoint=[
            "python", "-E", "-s", "/tests/stress_replay.py", "serve",
        ], command=["--delay-ms", "250"], mem_limit="1g",
        memswap_limit="1g", restart="no",
    )
    return {
        "services": services,
        "networks": {name: {"name": run.project + "_" + name,
                            "internal": True, "labels": {
                                "io.conix.stress-replay-owner": run.owner}}
                     for name in ("tests", "rpc")},
        "volumes": {name: {"name": run.project + "_" + name, "labels": {
                           "io.conix.stress-replay-owner": run.owner}}
                    for name in ("pgdata", "evidence", "results")},
    }


class ReplayOwnerContracts(unittest.TestCase):
    def test_exact_public_make_command_and_literals(self):
        import subprocess

        help_ = subprocess.run(["make", "-s", "help"], cwd=ROOT,
                               capture_output=True, text=True, check=True)
        self.assertIn("test/indexer/stress/replay", help_.stdout)
        source = (ROOT / "Makefile").read_text()
        operator = (ROOT / "tools/containers/operator.py").read_text()
        for name in ("EVIDENCE_DIR", "STRESS_REPLAY_DELAY_MS",
                     "STRESS_CHECKPOINT_CADENCE_MS", "STRESS_CATCHUP_TIMEOUT_SECONDS"):
            self.assertIn("$(value " + name + ")", source)
            self.assertIn('"' + name + '"', operator)
        self.assertIn("from tools.fixtures.stress_replay import run_replay", operator)

    def test_realistic_controls_require_three_finite_explicit_values(self):
        self.assertEqual(supplied(options())["stress_replay_delay_ms"], 250)
        cases = (
            {"stress_replay_delay_ms": "0"},
            {"stress_replay_delay_ms": "5001"},
            {"stress_replay_delay_ms": "$(shell echo bad)"},
            {"stress_checkpoint_cadence_ms": "99"},
            {"stress_checkpoint_cadence_ms": "inf"},
            {"stress_catchup_timeout_seconds": ""},
            {"stress_catchup_timeout_seconds": "3601"},
            {"evidence_dir": ""},
            {"context": ""},
            {"runner_image": "latest"},
        )
        for selection in cases:
            with self.subTest(selection=selection), self.assertRaises(OperationError):
                supplied(options(**selection))

    def test_declared_composition_blocks_external_network_privilege_and_mounts(self):
        run = ReplayAttempt.__new__(ReplayAttempt)
        run.project, run.owner, run.runner = "conix-replay-test", "owned", IMAGE
        run.images, run.delay = {"postgres": POSTGRES}, 250
        graph = graph_for(run)
        run.admit_graph(graph)
        violations = (
            lambda g: g["networks"]["rpc"].update(internal=False),
            lambda g: g["services"]["replay"]["networks"].update(egress={}),
            lambda g: g["services"]["replay"]["volumes"].append(
                {"type": "bind", "source": "/home", "target": "/host"}),
            lambda g: g["services"]["replay"].update(ports=[{"published": "8080"}]),
            lambda g: g["services"]["replay"].update(cap_add=["NET_ADMIN"]),
            lambda g: g["services"]["replay"].update(user="0:0"),
            lambda g: g["services"]["volume-init"].update(cap_add=["SYS_ADMIN"]),
            lambda g: g["services"]["replay"].update(command=["--delay-ms", "0"]),
            lambda g: g["volumes"]["evidence"]["labels"].update(
                {"io.conix.stress-replay-owner": "foreign"}),
        )
        for change in violations:
            with self.subTest(change=change), self.assertRaises(OperationError):
                broken = deepcopy(graph)
                change(broken)
                run.admit_graph(broken)

    def test_sampler_is_original_reader_only_without_egress_or_host_mounts(self):
        run = ReplayAttempt.__new__(ReplayAttempt)
        run.docker = SimpleNamespace(docker_command_prefix=["docker"])
        run.runner = IMAGE
        run.owner = "owner"
        run.project = "conix-test"
        run.name = "replay-name"
        run.records = {"indexer_image": IMAGE}
        run.controls = {"stress_checkpoint_cadence_ms": 1000,
                        "stress_catchup_timeout_seconds": 240}
        run.volume = Mock(side_effect=lambda suffix: "conix-test_" + suffix)
        reader = SimpleNamespace(image=IMAGE, volume="conix-inputs-" + "a" * 24)
        with patch("tools.fixtures.stress_replay.named.inspect_reader_prepared", return_value=reader):
            args = run.sample_arguments()
        for flag in ("--read-only", "--cap-drop", "--security-opt", "--network", "--mount"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--log-driver") + 1], "none")
        self.assertNotIn("egress", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--publish", args)
        self.assertNotIn("https://", " ".join(args))
        self.assertIn("/tests/stress_replay_probe.py", args)

    def test_catchup_is_not_an_invented_keep_pace_threshold(self):
        def measured(status, delay):
            return {
                "window_ns": 123, "first_checkpoint_ns": 22,
                "status": status, "reader": None if status == "incomplete_catchup" else {},
                "window": [
                    {"checkpoint_lag": None},
                    {"checkpoint_lag": 6},
                    {"checkpoint_lag": 12},
                ],
                "service": {"first_rpc_elapsed_ns": 2, "requests": 123,
                            "actual_wait_ns": 12345, "delay_ms": delay},
            }
        compared = comparison({"baseline": measured("complete", 0),
                               "slowdown": measured("incomplete_catchup", 250)})
        self.assertEqual(compared["baseline"]["final_sampled_lag"], 12)
        self.assertIsNone(compared["slowdown"]["sampled_backlog_change"])
        self.assertFalse(compared["slowdown"]["reader_verified"])
        self.assertEqual(compared["slowdown"]["catchup_status"], "incomplete_catchup")
        self.assertNotIn("pass", compared["baseline"])
        self.assertNotIn("capacity", compared["baseline"])


if __name__ == "__main__":
    unittest.main()
