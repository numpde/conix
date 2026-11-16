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


    def test_public_review_receipt_binds_both_reports_without_private_samples(self):
        from tools.fixtures.stress_artifacts import report_facts

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            folder = repository / "out/indexer/stress/runs/20261116-170000/fuji-pay-to-play/replay"
            folder.mkdir(parents=True)
            controls = supplied(options())
            reports = {}
            for name, latency in (("baseline", 0), ("slowdown", controls["stress_replay_delay_ms"])):
                target = folder / name
                target.mkdir()
                private = target / "measurement.json"
                private.write_text(json.dumps({"secret": "PRIVATE_DSN_EXAMPLE",
                                               "window": [{"head": 104, "checkpoint": 100}],
                                               "reader": {"events": 2}}))
                private.chmod(0o600)
                reports[name] = {
                    "status": "complete", "delay_ms": latency,
                    "indexer_image": IMAGE,
                    "measurement_sha256": hashlib.sha256(private.read_bytes()).hexdigest(),
                }
            (folder / "result.json").write_text('{"status":"complete"}')
            (folder / "result.json").chmod(0o600)
            def series(delay):
                return {
                    "window_samples": 4, "window_ns": 120000000000,
                    "first_checkpoint_ns": 1200000000, "first_rpc_ns": 300000000,
                    "max_sampled_lag": 14, "catchup_status": "complete",
                    "reader_verified": True, "request_count": 888,
                    "actual_wait_ns": 4000000000, "delay_ms": delay,
                }
            receipt = {
                "status": "complete", "cleanup": [],
                "controls": controls, "runs": reports,
                "completion_sha256": "c" * 64,
                "source_sha256": "a" * 64, "abi_sha256": "b" * 64,
                "comparison": {"baseline": series(0), "slowdown": series(250)},
            }
            public = report_facts(repository, folder, receipt)
            self.assertEqual(public["status"], "complete")
            self.assertEqual(set(public["runs"]), {"baseline", "slowdown"})
            self.assertEqual(public["runs"]["slowdown"]["report_sha256"],
                             reports["slowdown"]["measurement_sha256"])
            self.assertEqual(public["comparison"]["baseline"]["max_sampled_lag"], 14)
            self.assertNotIn("PRIVATE_DSN_EXAMPLE", json.dumps(public))
            self.assertNotIn("window", public["runs"]["baseline"])



    def test_admission_worker_retains_exact_exit_before_docker_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            run = ReplayAttempt.__new__(ReplayAttempt)
            run.folder = Path(directory)
            run.runner, run.owner = IMAGE, "owned"
            run.project = "conix-replay-owned"
            run.stage_attempted = False
            run.stage_retired = False
            run.records = {}
            run.payload = b"private tar bytes"
            run.sequence = 1
            run.admission = {"source_id": "c" * 64,
                             "coverage": {"start_block": 100, "final_block": 102}}
            run.compose = Mock()
            run.volume = Mock(return_value="conix-replay-owned_evidence")
            record = {"Id": "a" * 64, "State": {"Running": False, "ExitCode": 0}}
            run.stage_container = Mock(side_effect=[record, None])
            run.child = Mock(return_value=json.dumps({
                "status": "admitted", "source_id": "c" * 64,
                "start": 100, "final": 102,
            }).encode())
            operations = []
            def docker_run(*arguments, **_options):
                if arguments[:2] == ("container", "rm"):
                    self.assertEqual(operations, ["exit evidence retained"])
                    operations.append("removed exact ID")
                return ""
            run.docker = SimpleNamespace(docker_command_prefix=["docker"], run=docker_run)
            def retained(*_args, **_kwargs):
                operations.append("exit evidence retained")
                return {"exit_code": 0}
            with patch("tools.fixtures.stress_replay.retain_attached_exit", side_effect=retained):
                run.stage()
            self.assertEqual(operations, ["exit evidence retained", "removed exact ID"])
            self.assertTrue(run.stage_retired)
            self.assertEqual(run.records["admission"]["status"], "admitted")
            self.assertIn("--log-driver", run.child.call_args.args[0])
            self.assertNotIn("--rm", run.child.call_args.args[0])

    def test_uncertain_stage_retirement_preserves_evidence_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            run = ReplayAttempt.__new__(ReplayAttempt)
            run.folder = Path(directory)
            run.runner, run.owner = IMAGE, "owned"
            run.project = "conix-replay-owned"
            run.stage_attempted = False
            run.stage_retired = False
            run.touched = False
            run.profile_created = False
            run.consumer_started = False
            run.sampler_id = None
            run.consumer_retired = False
            run.records = {}
            run.payload = b"private tar bytes"
            run.sequence = 1
            run.admission = {"source_id": "c" * 64,
                             "coverage": {"start_block": 100, "final_block": 102}}
            run.compose = Mock()
            run.volume = Mock(return_value="conix-replay-owned_evidence")
            run.stage_container = Mock(return_value=None)
            run.child = Mock(return_value=json.dumps({
                "status": "admitted", "source_id": "c" * 64,
                "start": 100, "final": 102,
            }).encode())
            run.docker = SimpleNamespace(docker_command_prefix=["docker"],
                                         run=Mock(side_effect=AssertionError("Unowned deletion")))
            with self.assertRaises(OperationError):
                run.stage()
            failures = run.cleanup()
            self.assertTrue(failures)
            self.assertFalse(run.stage_retired)
            run.volume.assert_called_once_with("evidence")
            run.compose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
