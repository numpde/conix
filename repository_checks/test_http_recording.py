"""The focused WARC lane grants no external networking, binds or services."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tools.containers.client import OperationError
from tools.containers.operator import Docker
from tools.wheelhouse.policy import source_allowlist

ROOT = Path(__file__).resolve().parents[1]


class RecordingLaneContracts(unittest.TestCase):
    def test_actual_operator_argv_is_confined_and_selects_only_recording_tests(self):
        """Observe the launch arguments rather than validating a parallel recipe."""
        image = "sha256:" + "a" * 64
        calls = []
        operator = object.__new__(Docker)
        operator.require_rootless_engine = lambda: calls.append("rootless")
        operator.require_role = lambda selected, role: calls.append(("role", selected, role))
        operator.run = lambda *argv, **kwargs: calls.append((argv, kwargs))
        operator.test_http_recording(image)
        self.assertEqual(calls[:2], ["rootless", ("role", image, "test-runner")])
        argv, options = calls[2]
        for flag, value in (("--network", "none"), ("--pull", "never"), ("--user", "65532:65532"),
                            ("--cap-drop", "ALL"), ("--memory", "256m"), ("--memory-swap", "256m"),
                            ("--pids-limit", "64"), ("--cpus", "1"), ("--security-opt", "no-new-privileges:true"),
                            ("--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--read-only", argv)
        self.assertIn("--rm", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "test_http_recording.py")
        self.assertEqual(argv[argv.index("--entrypoint") + 2], image)
        self.assertEqual(options["timeout"], 120)
        self.assertTrue(all(flag not in argv for flag in ("--mount", "--volume", "--publish", "--privileged", "--env")))

    def test_bad_role_stops_before_any_worker_is_started(self):
        """A composite test image is required before creating container state."""
        operator = object.__new__(Docker)
        def refuse(*_args):
            raise OperationError("Selected image has a different role.")
        operator.require_rootless_engine = lambda: None
        operator.require_role = refuse
        operator.run = lambda *_args, **_kwargs: self.fail("Worker started before image admission.")
        with self.assertRaises(OperationError):
            operator.test_http_recording("sha256:" + "b" * 64)

    def test_public_lane_and_exact_offline_inputs_are_admitted(self):
        """The installed runtime never gains a test-only archive dependency."""
        help_text = subprocess.run(["make", "-s", "help"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertIn("test/http/recording/run", help_text)
        selected = {entry["destination"] for entry in source_allowlist(ROOT)}
        self.assertIn("tools/fixtures/http_recording.py", selected)
        self.assertIn("requirements/recording.lock", selected)
        runtime = (ROOT / "requirements/runtime.lock").read_text()
        self.assertNotIn("warcio", runtime)
        self.assertNotIn("six==", runtime)
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime_stage, tests = dockerfile.split("FROM indexer AS test-runner", 1)
        tests, evm = tests.split("FROM indexer AS evm-test-runner", 1)
        self.assertNotIn("requirements/recording.lock", runtime_stage)
        self.assertIn("source=requirements/recording.lock", tests)
        self.assertNotIn("requirements/recording.lock", evm)


    def test_build_selects_recording_wheels_only_for_the_test_runner(self):
        """Runtime and EVM builds must not acquire a recorder dependency closure."""
        from tools.wheelhouse.operations import SharedWheelhouse

        class SelectionObserved(Exception):
            pass

        wheelhouse = object.__new__(SharedWheelhouse)
        for target in ("indexer", "test-runner", "evm-test-runner"):
            with self.subTest(target=target):
                with patch.object(wheelhouse, "check", side_effect=SelectionObserved) as select:
                    with self.assertRaises(SelectionObserved):
                        wheelhouse.build(target)
                select.assert_called_once_with(include_recording=target == "test-runner")


    def test_rootful_engine_is_refused_before_image_or_worker_effects(self):
        """A context name never substitutes for observed daemon rootlessness."""
        operator = object.__new__(Docker)
        def refuse():
            raise OperationError("Selected daemon is not rootless.")
        operator.require_rootless_engine = refuse
        operator.require_role = lambda *_args: self.fail("Image admission started on a refused Engine.")
        operator.run = lambda *_args, **_kwargs: self.fail("Worker started on a refused Engine.")
        with self.assertRaises(OperationError):
            operator.test_http_recording("sha256:" + "a" * 64)
