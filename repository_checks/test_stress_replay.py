"""Stage C admission and resource-authority tests without Docker or provider access."""

from copy import deepcopy
import io
import hashlib
import json
from pathlib import Path
import tempfile
import tarfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.containers.client import OperationError
from tools.fixtures.stress_replay import ReplayAttempt, admitted_bundle, comparison, run_replay, supplied
from tools.fixtures.stress_capture import json_record
from tools.containers.diagnostics import PREFIX, admitted
from tools.fixtures.stress_artifacts import PRIVATE_RUNS

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
    def failed_run(self, root, *, publication=None, setup_failure=False, retirement=False):
        capture = root / "capture"
        completion = root / "completion"
        capture.mkdir(mode=0o700)
        completion.mkdir(mode=0o700)
        (capture / "abi.json").write_bytes(b"[]")
        (capture / "manifest.json").write_text(json.dumps({"observer": {"samples": [{"header": {"height": 100}}]}}))
        canonical = b"version = 1\n[chain]\nchain_id = 43113\n"
        (completion / "catalog.toml").write_bytes(b"version = 1\n[chain]\nchain_id = 1\n")
        files = {"catalog.toml": canonical, "abi.json": b"[]",
                 "manifest.json": (capture / "manifest.json").read_bytes(),
                 "source.json": b"{}", "deployment.toml": b'docker_context = "agentic"\n'}
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        admission = {"source_id": "c" * 64, "completion_sha256": "d" * 64, "capture_revision": "e" * 40}
        diagnostic = PREFIX + b'{"phase":"verification","cause":"RpcUnavailable","effect":"unconfirmed"}'
        first = OperationError("PRIVATE_PRODUCER_TEXT", exit_code=17, diagnostic=diagnostic)
        run = SimpleNamespace(records={}, preflight=Mock(side_effect=first), cleanup=Mock(return_value=[]))
        if setup_failure or retirement:
            run.records["status"] = "complete"
            run.preflight = Mock()
            run.stage, run.boot = Mock(), Mock()
            run.measured = Mock(return_value={"status": "complete"})
            if retirement:
                def cleanup():
                    run.records["consumer_failure"] = {"diagnostic": admitted(diagnostic), "exit_code": 17}
                    return []
                run.cleanup = cleanup
        constructor = Mock(return_value=run)
        if setup_failure:
            constructor.side_effect = [run, PermissionError("PRIVATE_SLOWDOWN_SETUP")]
        docker = SimpleNamespace(require_rootless_engine=lambda: None, require_role=lambda *args: None,
                                 require_image=lambda *args: None)
        def write(path, record):
            if publication == "result" and path.name == "result.json":
                raise PermissionError("PRIVATE_RESULT_PATH")
            json_record(path, record)
        report = Mock(return_value=root / "qualification/report.json")
        if publication == "review":
            report.side_effect = PermissionError("PRIVATE_REPORT_PATH")
        with patch("tools.fixtures.stress_replay.ROOT", root), \
             patch("tools.fixtures.stress_replay.admitted_bundle", return_value=(payload.getvalue(), hashes, admission, {}, {"docker_context": "agentic"})), \
             patch("tools.fixtures.stress_replay.source_subject", return_value="fixture"), \
             patch("tools.fixtures.stress_replay.select_endpoint", return_value="selected"), \
             patch("tools.fixtures.stress_replay.ReplayAttempt", constructor), \
             patch("tools.fixtures.stress_replay.json_record", side_effect=write), \
             patch("tools.fixtures.stress_replay.publish_report", report), \
             patch("tools.fixtures.stress_replay.progress") as shown:
            with self.assertRaises(OperationError) as caught:
                run_replay(options(capture_dir=str(capture), evidence_dir=str(completion)),
                           client_cls=lambda *args: docker, pinned_images=lambda: {"postgres": POSTGRES})
        folder = next((root / PRIVATE_RUNS).glob("*/fixture/replay"))
        return caught.exception, folder, shown, canonical

    def test_result_publication_failure_preserves_the_original_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            failure, _, shown, _ = self.failed_run(Path(directory), publication="result")
            self.assertEqual(admitted(failure.diagnostic)["cause"], "RpcUnavailable")
            self.assertEqual(failure.exit_code, 17)
            messages = "\n".join(call.args[0] for call in shown.call_args_list)
            self.assertIn("publication failed", messages)
            self.assertNotIn("PRIVATE_", messages)

    def test_review_publication_failure_is_reported_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            failure, _, shown, _ = self.failed_run(Path(directory), publication="review")
            self.assertEqual(admitted(failure.diagnostic)["cause"], "RpcUnavailable")
            messages = "\n".join(call.args[0] for call in shown.call_args_list)
            self.assertIn("publication failed", messages)
            self.assertNotIn("PRIVATE_", messages)

    def test_public_catalog_comes_from_the_admitted_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            _, folder, _, canonical = self.failed_run(Path(directory))
            self.assertEqual((folder / "catalog.toml").read_bytes(), canonical)

    def test_slowdown_setup_failure_retains_the_completed_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            failure, folder, _, _ = self.failed_run(Path(directory), setup_failure=True)
            result = json.loads((folder / "result.json").read_bytes())
            self.assertEqual(result["runs"]["baseline"]["status"], "complete")
            self.assertEqual(result["runs"]["slowdown"]["status"], "unavailable")
            self.assertEqual(admitted(failure.diagnostic)["cause"], "PermissionError")

    def test_retired_original_consumer_failure_is_not_a_successful_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            failure, folder, _, _ = self.failed_run(Path(directory), retirement=True)
            result = json.loads((folder / "result.json").read_bytes())
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(admitted(failure.diagnostic)["cause"], "RpcUnavailable")

    def test_common_trigger_does_not_wait_for_head_release_acknowledgement(self):
        """Slow release control must not hold the original CLI launch gate shut."""
        class StopAfterLaunch(Exception):
            pass
        run = ReplayAttempt.__new__(ReplayAttempt)
        run.records = {}
        run.source = {"initial_head": 100}
        run.replay_id = "a" * 64
        run.sampler = Mock()
        run.status = Mock(return_value={"head": 100, "released": False, "elapsed_ns": 0})
        launched = threading.Event()
        run.make = Mock(side_effect=lambda action: launched.set())
        run.original = Mock(side_effect=StopAfterLaunch)
        def release(*args, **kwargs):
            self.assertTrue(launched.wait(0.2), "Original launch waited for the head-release response.")
            return json.dumps({"released_ns": 100})
        run.docker = SimpleNamespace(run=release)
        with patch("tools.fixtures.stress_replay.time.sleep"), self.assertRaises(StopAfterLaunch):
            run.measured()
        self.assertTrue(launched.is_set())

    def test_broken_progress_does_not_interrupt_owned_cleanup(self):
        run = ReplayAttempt.__new__(ReplayAttempt)
        run.profile_created = False
        run.sampler_id = None
        run.touched, run.stage_attempted, run.stage_retired = True, False, False
        run.replay_id = "a" * 64
        run.result_exported = True
        run.records = {}
        run.phase = "cleanup"
        run.verify_owned_graph = Mock()
        run.owned = Mock(return_value=(run.replay_id, {"State": {"Running": False, "ExitCode": 7}}))
        run.docker = SimpleNamespace(run=Mock())
        run.compose = Mock()
        run.volume = Mock(side_effect=lambda name: "owned-" + name)
        facts = {"exit_code": 7, "oom_killed": False, "diagnostic": None}
        with patch("tools.fixtures.stress_replay.retain_exit", return_value=facts), \
             patch("tools.fixtures.stress_replay.progress", side_effect=BrokenPipeError):
            self.assertEqual(run.cleanup(), [])
        run.compose.assert_called_once_with("down", "--remove-orphans", "--timeout", "30", timeout=90)
        self.assertEqual(run.docker.run.call_count, 3)

    def test_sampler_exit_evidence_is_retained_before_removal(self):
        run = ReplayAttempt.__new__(ReplayAttempt)
        run.profile_created, run.touched = False, False
        run.sampler_id = "a" * 64
        run.records = {}
        run.sampler_state = Mock(return_value={"State": {"Running": False}})
        retained = threading.Event()
        def remove(*args, **kwargs):
            self.assertTrue(retained.is_set())
        run.docker = SimpleNamespace(run=remove)
        def keep(*args):
            retained.set()
            return {"exit_code": 0}
        with patch("tools.fixtures.stress_replay.retain_exit", side_effect=keep):
            self.assertEqual(run.cleanup(), [])
        self.assertTrue(run.sampler_retired)

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
        self.assertEqual(args[args.index("--log-driver") + 1], "local")
        for option in ("max-size=64k", "max-file=1", "compress=false"):
            self.assertIn(option, args)
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

            # A saved measurement is not verified until its hash was retained.
            reports["slowdown"].pop("measurement_sha256")
            receipt["status"] = "unavailable"
            receipt["comparison"] = None
            public = report_facts(repository, folder, receipt)
            self.assertEqual(public["status"], "unavailable")
            self.assertEqual(set(public["runs"]), {"baseline"})

            receipt["status"] = "complete"
            with self.assertRaises(OperationError):
                report_facts(repository, folder, receipt)



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
