"""Behavioral contract for the operation-owned Docker client; never contact a daemon."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools/container.py"
spec = importlib.util.spec_from_file_location("conix_container_operator", TOOL)
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["CONIX_FAKE_LOG"], "a") as output:
    output.write(json.dumps({"args": args, "docker_host": os.getenv("DOCKER_HOST"),
                             "compose_file": os.getenv("COMPOSE_FILE"),
                             "config": os.getenv("DOCKER_CONFIG")}) + "\n")
if args[:2] == ["context", "ls"]:
    print("science\tunix:///tmp/science-docker.sock")
    print("remote\ttcp://example.invalid:2375")
elif "compose" in args and "config" in args:
    print(json.dumps({"services": {
        "tests": {"image": os.environ["CONIX_TEST_IMAGE"],
                  "read_only": True, "user": "65532:65532"},
        "postgres": {"read_only": True, "image": os.environ["CONIX_POSTGRES_IMAGE"]}}}))
elif "compose" in args and "up" in args and os.getenv("CONIX_FAKE_UP_FAIL"):
    sys.exit(3)
elif "compose" in args and "down" in args and os.getenv("CONIX_FAKE_DOWN_FAIL"):
    sys.exit(5)
'''


class LocalDockerOperatorTests(unittest.TestCase):
    def test_endpoint_only_accepts_local_unix_socket(self):
        for value in ("unix:///run/docker.sock", "unix:///tmp/other.sock"):
            self.assertTrue(operator.local_socket(value))
        for value in ("tcp://localhost:2375", "ssh://host", "unix://relative",
                      "unix:///", "unix:///run/docker.sock?query=1"):
            self.assertFalse(operator.local_socket(value))

    def test_invalid_or_ambient_context_does_not_choose_an_engine(self):
        with patch.object(operator, "command", return_value="science\tunix:///tmp/science.sock\n"):
            with self.assertRaises(operator.OperationError):
                operator.select_endpoint("unknown", "command line")
            with patch("builtins.open", side_effect=OSError):
                with self.assertRaises(operator.OperationError):
                    operator.select_endpoint("science", "environment")

    def run_fake(self, *, fail_up=False, fail_down=False):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fake = directory / "docker"
            fake.write_text(FAKE)
            fake.chmod(0o755)
            log = directory / "docker.jsonl"
            env = os.environ.copy()
            env["PATH"] = str(directory) + os.pathsep + env["PATH"]
            env["CONIX_FAKE_LOG"] = str(log)
            env["DOCKER_HOST"] = "tcp://malicious.invalid:2375"
            env["DOCKER_CONTEXT"] = "remote"
            env["COMPOSE_FILE"] = "/tmp/foreign.yml"
            if fail_up:
                env["CONIX_FAKE_UP_FAIL"] = "1"
            if fail_down:
                env["CONIX_FAKE_DOWN_FAIL"] = "1"
            image = "sha256:" + "a" * 64
            run = subprocess.run([
                sys.executable, str(TOOL),
                "--context", "science", "--origin", "command line",
                "tests/container", "--test-image", image,
            ], env=env, capture_output=True, text=True, timeout=15)
            records = [json.loads(line) for line in log.read_text().splitlines()]
            return run, records

    def test_run_pins_selected_engine_and_cleans_unique_project(self):
        completed, calls = self.run_fake()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(any("up" in item["args"] for item in calls))
        self.assertTrue(any("down" in item["args"] for item in calls))
        for item in calls:
            self.assertIsNone(item["docker_host"])
            self.assertIsNone(item["compose_file"])
            if "context" not in item["args"]:
                self.assertIn("unix:///tmp/science-docker.sock", item["args"])
                self.assertTrue(item["config"])
        projects = [item["args"][item["args"].index("-p") + 1]
                    for item in calls if "compose" in item["args"]]
        self.assertEqual(len(set(projects)), 1)
        self.assertTrue(projects[0].startswith("conix-tier0-"))

    def test_failed_test_still_tears_down(self):
        completed, calls = self.run_fake(fail_up=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(any("up" in item["args"] for item in calls))
        self.assertTrue(any("down" in item["args"] for item in calls))

    def test_uncertain_teardown_is_reported_as_failure(self):
        completed, calls = self.run_fake(fail_down=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Cleanup unconfirmed", completed.stderr)
        self.assertTrue(any("down" in item["args"] for item in calls))


if __name__ == "__main__":
    unittest.main()
