"""Stress capture admission, timing and retirement are testable without Docker."""

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.containers.client import OperationError
from tools.fixtures.process import CampaignError
import tools.fixtures.stress_capture as stress_capture
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

    def test_git_revision_uses_sha1_or_sha256_not_docker_identity(self):
        from tools.fixtures.stress_capture import HASH, ID

        self.assertTrue(HASH.fullmatch("a" * 40))
        self.assertTrue(HASH.fullmatch("a" * 64))
        self.assertFalse(ID.fullmatch("a" * 40))
        for bad in ("a" * 39, "a" * 41, "z" * 40, "a" * 65):
            with self.subTest(revision=bad[:8], length=len(bad)):
                self.assertIsNone(HASH.fullmatch(bad))

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

    def test_application_export_uses_gateway_retirement(self):
        """The application stream belongs to the gateway, not an application service."""
        fixture = StressCapture.__new__(StressCapture)
        fixture.retired = {"gateway"}
        fixture.owned_volume = Mock(side_effect=LookupError("volume admission reached"))
        with self.assertRaises(LookupError):
            fixture.export("application")
        fixture.owned_volume.assert_called_once_with("application")

    def test_engine_lifecycle_events_must_cover_one_uninterrupted_process(self):
        from datetime import datetime, timedelta, timezone

        fixture = StressCapture.__new__(StressCapture)
        fixture.docker = Mock()
        identity = "a" * 64
        started = datetime.now(timezone.utc)
        ended = started + timedelta(seconds=5)

        def feed(actions):
            return "\n".join(json.dumps({
                "Action": action, "Type": "container",
                "Actor": {"ID": identity}, "timeNano": (i + 1) * 1_000_000_000,
            }) for i, action in enumerate(actions))
        result = fixture.history(identity, started, ended, feed(["start"]))
        self.assertEqual(result["events"][0]["action"], "start")
        for actions in ([], ["die"], ["start", "die"], ["start", "restart"], ["start", "start"]):
            with self.subTest(actions=actions):
                with self.assertRaises(OperationError):
                    fixture.history(identity, started, ended, feed(actions))

    def test_live_lifecycle_receipt_survives_history_loss_and_retires_child(self):
        """Keep the subscribed start event even when later history is unavailable."""
        import sys
        from datetime import datetime, timezone

        original_popen = subprocess.Popen
        identity = "a" * 64
        event = json.dumps({"Action": "start", "Type": "container",
                            "Actor": {"ID": identity}, "timeNano": 1_000_000_000})
        with tempfile.TemporaryDirectory() as directory:
            fixture = StressCapture.__new__(StressCapture)
            fixture.folder = Path(directory)
            fixture.selection = {"stress_duration_seconds": 1}
            fixture.records = {}
            fixture.docker = Mock()
            fixture.docker.docker_command_prefix = ["docker", "--host", "unix:///selected"]
            fixture.docker.docker_environment = {}
            fixture.docker.run.side_effect = AssertionError("Retrospective history is unavailable")

            def stream(argv, **kwargs):
                self.assertIn("events", argv)
                self.assertIn("container=" + identity, argv)
                return original_popen(
                    [sys.executable, "-c", "import time; print(" + repr(event) +
                     ", flush=True); time.sleep(60)"], **kwargs,
                )

            failure = RuntimeError("original observation failure")
            with patch("tools.fixtures.stress_capture.subprocess.Popen", side_effect=stream):
                with self.assertRaises(RuntimeError) as caught:
                    with fixture.lifecycle_stream(identity, datetime.now(timezone.utc)) as child:
                        self.assertIn(identity, fixture.read_lifecycle_stream())
                        raise failure
            self.assertIs(caught.exception, failure)
            self.assertIsNotNone(child.poll())
            self.assertEqual((fixture.folder / "indexer-events.jsonl").stat().st_mode & 0o777, 0o600)
            fixture.docker.run.assert_not_called()

    def test_nonzero_recorder_exit_is_retired_but_not_successful_capture(self):
        fixture = StressCapture.__new__(StressCapture)
        fixture.profile_created = False
        fixture.fixture_attempted = True
        fixture.consumer_retired = False
        fixture.records = {}
        fixture.retired = set()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture.docker = Mock()
        fixture.docker.working_dir = Path(temporary.name)
        fixture.docker.container_logs.return_value = (b"", b"")
        fixture.container = Mock(side_effect=lambda service: (
            ("a" if service == "gateway" else "b") * 64,
            {"Id": ("a" if service == "gateway" else "b") * 64,
             "Image": "sha256:" + "c" * 64, "RestartCount": 0,
             "State": {"Running": False, "ExitCode": (0 if service == "gateway" else 17),
                       "OOMKilled": False, "StartedAt": "2026-11-16T00:00:00Z",
                       "FinishedAt": "2026-11-16T00:00:01Z"}},
        ))
        with self.assertRaises(OperationError):
            fixture.stop()
        self.assertTrue(fixture.consumer_retired)
        self.assertEqual(fixture.retired, {"gateway", "observer"})
        self.assertEqual(fixture.records["worker_exits"]["observer"], 17)

    def test_partial_recorder_receipt_reports_bounded_worker_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StressCapture.__new__(StressCapture)
            fixture.folder = Path(directory)
            fixture.archive_exported = set()
            fixture.selection = {"stress_exchanges": 4096}
            fixture.records = {"export": {"observer": {"status": "unconfirmed"}}}
            (fixture.folder / "observer-receipt.json").write_text(json.dumps({
                "status": "partial", "exchanges": 3, "transport_stopped": True,
                "failure": "ReadTimeout",
            }))
            (fixture.folder / "observer-receipt.json").chmod(0o600)
            fixture.report_archive_outcomes()
            observed = fixture.records["archive_evidence"]["observer"]
            self.assertEqual(observed["archive"], "unconfirmed")
            self.assertEqual(observed["recorder_receipt"], "partial")
            self.assertEqual(observed["failure"], "ReadTimeout")

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



class CaptureFailureReportingContracts(unittest.TestCase):
    """Report admitted primary failure facts separately from resource cleanup."""

    def run_failed_capture(self, *, primary=None, phase="Stress deployment/up", cleanup=(), archive_evidence=None):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            private = base / "private"
            private.mkdir()
            run = Mock()
            run.folder = private
            run.phase = phase
            run.records = {"capture": "unconfirmed", "export": {}, "cleanup": []}
            run.fixture_attempted = False
            run.runner = None
            run.retired = set()
            run.child.side_effect = [b"a" * 40 + b"\n", b""]
            run.observe.side_effect = primary
            run.cleanup.return_value = list(cleanup)
            if archive_evidence is not None:
                run.report_archive_outcomes.side_effect = lambda: run.records.update(
                    archive_evidence=archive_evidence)
            if primary is None:
                run.complete.side_effect = lambda: run.records.update(capture="complete")
            shown = []
            config = SimpleNamespace(context="agentic", wheelhouse="",
                                     **vars(parameters()))
            with (
                patch.object(stress_capture, "ROOT", base),
                patch.object(stress_capture.os, "geteuid", return_value=1000),
                patch.object(stress_capture, "protect_source_profile"),
                patch.object(stress_capture, "selected_source",
                             return_value=({}, {"docker_context": "agentic", "wheelhouse": "/pool"}, b"", b"")),
                patch.object(stress_capture, "campaign_lock", return_value=nullcontext()),
                patch.object(stress_capture, "cancellation", return_value=nullcontext()),
                patch.object(stress_capture, "select_endpoint", return_value="unix:///selected"),
                patch.object(stress_capture, "StressCapture", return_value=run),
                patch.object(stress_capture, "progress", side_effect=shown.append),
            ):
                with self.assertRaises(OperationError) as caught:
                    stress_capture.run_capture(
                        config,
                        client_cls=lambda *_args: SimpleNamespace(require_rootless_engine=lambda: None),
                        pinned_images=lambda: {},
                    )
            manifest = json.loads((private / "manifest.json").read_text())
            return str(caught.exception), shown, manifest, caught.exception.__cause__

    def test_subprocess_failure_reports_original_phase_cause_and_exit_last(self):
        first = CampaignError("PRIVATE_PROVIDER_BODY", exit_code=17)
        message, terminal, manifest, cause = self.run_failed_capture(
            primary=first, phase="Stress deployment/up",
            cleanup=("fixture: OSError",),
        )
        self.assertIn("phase=Stress deployment/up", message)
        self.assertIn("cause=CampaignError", message)
        self.assertIn("exit=17", message)
        self.assertNotIn("PRIVATE_PROVIDER_BODY", message)
        self.assertIs(cause, first)
        self.assertEqual(manifest["primary_failure"], "CampaignError")
        self.assertTrue(any("cleanup" in line.lower() and "OSError" in line for line in terminal))
        self.assertEqual(manifest["failed_phase"], "Stress deployment/up")

    def test_admitted_producer_cause_survives_without_stderr_prose(self):
        diagnostic = (b'CONIX-FAILURE-V1 '
                      b'{"phase":"schema_init","cause":"StorageUnavailable","effect":"unconfirmed"}')
        first = OperationError("PRIVATE_DATABASE_DETAIL", exit_code=7, diagnostic=diagnostic)
        message, _, _, cause = self.run_failed_capture(
            primary=first, phase="Stress deployment/database/schema/init",
        )
        self.assertIn("phase=Stress deployment/database/schema/init", message)
        self.assertIn("cause=StorageUnavailable", message)
        self.assertIn("exit=7", message)
        self.assertNotIn("PRIVATE_DATABASE_DETAIL", message)
        self.assertIs(cause, first)

    def test_recorder_timeout_category_is_visible_without_private_prose(self):
        message, _, _, _ = self.run_failed_capture(
            primary=OperationError("PRIVATE_UPSTREAM_REPLY"),
            phase="observer done",
            archive_evidence={
                "observer": {
                    "recorder_receipt": "partial", "archive": "unconfirmed",
                    "failure": "ReadTimeout", "transport_stopped": True,
                },
            },
        )
        self.assertIn("phase=observer done", message)
        self.assertIn("recorder_observer_failure=ReadTimeout", message)
        self.assertNotIn("PRIVATE_UPSTREAM_REPLY", message)

    def test_cleanup_only_has_clear_phase_and_cause(self):
        message, terminal, manifest, cause = self.run_failed_capture(
            cleanup=("fixture: OSError",),
        )
        self.assertIn("phase=cleanup", message)
        self.assertIn("OSError", message)
        self.assertIn("cleanup", terminal[-1].lower())
        self.assertEqual(manifest["cleanup"], ["fixture: OSError"])
        self.assertIsNone(cause)


if __name__ == "__main__":
    unittest.main()
