"""Behavioral contract for the operation-owned Docker client; never contact a daemon."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
from tools.containers import operator

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
elif "info" in args and "--format" in args:
    options = ["name=seccomp,profile=builtin"]
    if not os.getenv("CONIX_FAKE_ROOTFUL_ENGINE"):
        options.append("name=rootless")
    print(json.dumps(options))
elif "image" in args and "inspect" in args:
    if any("io.conix.image-role" in item for item in args):
        print(os.getenv("CONIX_FAKE_ROLE", "indexer"))
    elif os.getenv("CONIX_FAKE_MISSING_RESOURCE"):
        sys.exit(3)
    else:
        print("sha256:" + "a" * 64)
elif "volume" in args and "inspect" in args:
    volume = args[-1]
    print(json.dumps([{"Labels": {"io.conix.input-token": volume.removeprefix("conix-inputs-")}}]))
elif "network" in args and "inspect" in args:
    print(args[-1])
elif "compose" in args and "config" in args:
    if any("runtime.yml" in item for item in args):
        named_volume = os.environ["CONIX_INPUT_VOLUME"]
        item = {"image": os.environ["CONIX_INDEXER_IMAGE"], "user": "65532:65532",
                "read_only": True, "pull_policy": "never", "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "networks": {"database": None, "rpc": None},
                "volumes": [{"source": named_volume, "type": "volume",
                             "target": "/run/conix", "read_only": True}]}
        if os.getenv("CONIX_FAKE_BAD_RUNTIME_CAP"):
            item["cap_add"] = ["SYS_ADMIN"]
        if os.getenv("CONIX_FAKE_BAD_RUNTIME_PRIVILEGES"):
            item["security_opt"] = []
        if os.getenv("CONIX_FAKE_BAD_RUNTIME_NETWORK"):
            item["networks"] = {"database": None}
        if os.getenv("CONIX_FAKE_BAD_RUNTIME_BIND"):
            item["volumes"].append({"type": "bind", "source": "/tmp", "target": "/tmp"})
        print(json.dumps({"services": {"indexer": item}, "networks": {
            "database": {"name": os.environ["CONIX_DATABASE_NETWORK"]},
            "rpc": {"name": os.environ["CONIX_RPC_NETWORK"]}}}))
    else:
        print(json.dumps({"services": {
            "tests": {"image": os.environ["CONIX_TEST_RUNNER_IMAGE"],
                      "read_only": True, "user": "65532:65532"},
            "postgres": {"read_only": True, "user": "999:999", "image": os.environ["CONIX_POSTGRES_IMAGE"]},
            "bootstrap": {"read_only": True, "user": "999:999", "image": os.environ["CONIX_POSTGRES_IMAGE"]},
            "initialize": {"read_only": True, "user": "65532:65532", "image": os.environ["CONIX_TEST_RUNNER_IMAGE"]}},
            "networks": {"tests": {"internal": not bool(os.getenv("CONIX_FAKE_BAD_NETWORK"))}}}))
elif "run" in args and "compose" not in args and args[args.index("run") + 1:]:
    # Runtime deployment checks execute only a non-root, read-only, one-shot
    # installed-Python input inspection. This is a known operation, not a
    # blanket acceptance of arbitrary docker run commands.
    tail = args[args.index("run") + 1:]
    if (
        "--rm" not in tail
        or "--network" not in tail
        or "none" not in tail
        or "--read-only" not in tail
        or "--entrypoint" not in tail
        or tail[tail.index("--entrypoint") + 1] != "python"
        or "-c" not in tail
    ):
        print("Recording Docker refuses an unrecognized container run.", file=sys.stderr)
        sys.exit(97)
elif "compose" in args:
    # The recording client may simulate only the operations exercised by the
    # current operator. New Docker commands require an explicit fixture update.
    tail = args[args.index("compose") + 1:]
    supported = ("up", "down", "stop", "ps", "run")
    action = next((part for part in tail if part in supported), None)
    if action is None:
        print("Recording Docker refuses an unknown Compose command.", file=sys.stderr)
        sys.exit(97)
    if action == "up" and os.getenv("CONIX_FAKE_UP_FAIL"):
        sys.exit(3)
    if action == "down" and os.getenv("CONIX_FAKE_DOWN_FAIL"):
        sys.exit(5)
else:
    print("Recording Docker refuses an unknown command.", file=sys.stderr)
    sys.exit(97)
'''


class LocalDockerOperatorTests(unittest.TestCase):
    def test_recording_docker_refuses_unknown_operations(self):
        # A fake that exits zero for an unrecognized command can certify a
        # dangerous new side effect as safe simply because it did not inspect it.
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "docker"
            fake.write_text(FAKE, encoding="utf-8")
            fake.chmod(0o755)
            env = os.environ.copy()
            env["CONIX_FAKE_LOG"] = str(Path(directory) / "trace.jsonl")
            for arguments in (
                ("system", "prune", "--all"),
                ("image", "rm", "unreviewed"),
                ("compose", "rm", "--force"),
                ("container", "exec", "unknown", "sh"),
            ):
                with self.subTest(arguments=arguments):
                    completed = subprocess.run(
                        [sys.executable, str(fake), *arguments],
                        env=env, capture_output=True, text=True, timeout=5,
                    )
                    self.assertNotEqual(completed.returncode, 0,
                        "Unknown Docker command was silently accepted by the fake.")

    def test_endpoint_only_accepts_local_unix_socket(self):
        for value in ("unix:///run/docker.sock", "unix:///tmp/other.sock"):
            self.assertTrue(operator.local_socket(value))
        for value in ("tcp://localhost:2375", "ssh://host", "unix://relative",
                      "unix:///", "unix:///run/docker.sock?query=1"):
            self.assertFalse(operator.local_socket(value))

    def test_host_root_is_refused_before_engine_discovery(self):
        with patch.object(operator.os, "geteuid", return_value=0), patch.object(
            operator, "command", side_effect=AssertionError("Docker contacted before host identity admission")
        ):
            with self.assertRaises(operator.OperationError) as error:
                operator.select_endpoint("science", "command line")
        self.assertIn("root", str(error.exception).lower())

    def test_invalid_or_ambient_context_does_not_choose_an_engine(self):
        with patch.object(operator, "command", return_value="science\tunix:///tmp/science.sock\n"):
            with self.assertRaises(operator.OperationError):
                operator.select_endpoint("unknown", "command line")
            with patch("builtins.open", side_effect=OSError):
                with self.assertRaises(operator.OperationError):
                    operator.select_endpoint("science", "environment")

    def run_fake(self, *, fail_up=False, fail_down=False, bad_network=False):
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
            if bad_network:
                env["CONIX_FAKE_BAD_NETWORK"] = "1"
            env["CONIX_FAKE_ROLE"] = "test-runner"
            image = "sha256:" + "a" * 64
            run = subprocess.run([
                sys.executable, "-m", "tools.containers.operator",
                "--context", "science", "--origin", "command line",
                "test/postgres/run", "--runner-image", image,
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

    def test_external_test_network_is_rejected_before_creation(self):
        completed, calls = self.run_fake(bad_network=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(any("up" in item["args"] for item in calls))
        self.assertFalse(any("down" in item["args"] for item in calls))
        self.assertIn("network", completed.stderr)

    def run_runtime_fake(self, *, bad_bind=False, operation="apply", missing_resources=False,
                         bad_cap=False, bad_privileges=False, bad_runtime_network=False,
                         rootful=False):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fake = directory / "docker"
            fake.write_text(FAKE)
            fake.chmod(0o755)
            log = directory / "docker.jsonl"
            env = os.environ.copy()
            env["PATH"] = str(directory) + os.pathsep + env["PATH"]
            env["CONIX_FAKE_LOG"] = str(log)
            env["CONIX_FAKE_ROLE"] = "indexer"
            env["DOCKER_HOST"] = "tcp://malicious.invalid:2375"
            env["COMPOSE_FILE"] = "/tmp/foreign.yml"
            if bad_bind:
                env["CONIX_FAKE_BAD_RUNTIME_BIND"] = "1"
            if bad_cap:
                env["CONIX_FAKE_BAD_RUNTIME_CAP"] = "1"
            if bad_privileges:
                env["CONIX_FAKE_BAD_RUNTIME_PRIVILEGES"] = "1"
            if bad_runtime_network:
                env["CONIX_FAKE_BAD_RUNTIME_NETWORK"] = "1"
            if rootful:
                env["CONIX_FAKE_ROOTFUL_ENGINE"] = "1"
            if missing_resources:
                env["CONIX_FAKE_MISSING_RESOURCE"] = "1"
            result = subprocess.run([
                sys.executable, "-m", "tools.containers.operator",
                "--context", "science", "--origin", "command line",
                "deployment/" + operation, "--deployment", "pilot",
                "--indexer-image", "sha256:" + "a" * 64,
                "--volume", "conix-inputs-" + "b" * 24,
                "--database-network", "db-conix", "--rpc-network", "rpc-conix",
            ], env=env, capture_output=True, text=True, timeout=15)
            return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_runtime_reuses_engine_and_never_builds_or_pulls(self):
        run, calls = self.run_runtime_fake()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(any("stop" in item["args"] for item in calls))
        self.assertTrue(any("up" in item["args"] for item in calls))
        self.assertFalse(any("build" in item["args"] or "pull" in item["args"] for item in calls))
        projects = [item["args"][item["args"].index("-p") + 1]
                    for item in calls if "compose" in item["args"]]
        self.assertEqual(set(projects), {"conix-pilot"})
        for item in calls:
            if "context" not in item["args"]:
                self.assertIn("unix:///tmp/science-docker.sock", item["args"])

    def test_runtime_stop_does_not_need_removed_external_resources(self):
        result, calls = self.run_runtime_fake(operation="remove", missing_resources=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("stop" in item["args"] for item in calls))
        self.assertTrue(any("down" in item["args"] for item in calls))
        self.assertFalse(any("image" in item["args"] and "inspect" in item["args"] for item in calls))
        self.assertFalse(any("volume" in item["args"] and "inspect" in item["args"] for item in calls))

    def test_runtime_start_rejects_rootful_engine_without_side_effects(self):
        result, calls = self.run_runtime_fake(rootful=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rootless", result.stderr)
        self.assertTrue(any("info" in entry["args"] for entry in calls))
        self.assertFalse(any("up" in entry["args"] or "stop" in entry["args"] for entry in calls))

    def test_owner_init_uses_only_temporary_private_input_and_database_network(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = operator.Docker("unix:///tmp/science-docker.sock", Path(directory))
            volume = "conix-inputs-" + "c" * 24
            calls = []

            def call(*args, **kwargs):
                calls.append(args)
                if args[:2] == ("network", "inspect"):
                    return "db-conix\n"
                return None

            with patch.object(docker, "require_rootless_engine"), patch.object(
                docker, "require_image"
            ), patch.object(docker, "stage_inputs", return_value=volume) as stage, patch.object(
                docker, "remove_inputs"
            ) as cleanup, patch.object(docker, "run", side_effect=call):
                docker.initialize_schema(
                    "sha256:" + "a" * 64,
                    "/home/operator/config.toml",
                    "/home/operator/owner.dsn",
                    "db-conix",
                )
                stage.assert_called_once_with(
                    "sha256:" + "a" * 64,
                    "/home/operator/config.toml",
                    "/home/operator/owner.dsn",
                    announce=False,
                )
                cleanup.assert_called_once_with(volume)
            command = next(args for args in calls if args and args[0] == "run")
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop", command)
            self.assertIn("no-new-privileges:true", command)
            self.assertEqual(command[command.index("--network") + 1], "db-conix")
            self.assertEqual(command[command.index("--user") + 1], "65532:65532")
            self.assertIn("init", command)
            self.assertIn("/run/conix-owner/connection.dsn", command)
            self.assertNotIn("/home/operator/owner.dsn", command)
            self.assertFalse(any("pull" in args or "build" in args for args in calls))

    def test_owner_init_failure_still_retires_temporary_secret_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = operator.Docker("unix:///tmp/science-docker.sock", Path(directory))
            volume = "conix-inputs-" + "c" * 24

            def call(*args, **kwargs):
                if args[:2] == ("network", "inspect"):
                    return "db-conix\n"
                if args and args[0] == "run":
                    raise operator.OperationError("Owner init outcome unknown.")
                return None

            with patch.object(docker, "require_rootless_engine"), patch.object(
                docker, "require_image"
            ), patch.object(docker, "stage_inputs", return_value=volume), patch.object(
                docker, "remove_inputs"
            ) as cleanup, patch.object(docker, "run", side_effect=call):
                with self.assertRaises(operator.OperationError):
                    docker.initialize_schema(
                        "sha256:" + "a" * 64,
                        "/home/operator/config.toml",
                        "/home/operator/owner.dsn",
                        "db-conix",
                    )
                cleanup.assert_called_once_with(volume)

    def test_runtime_start_still_refuses_missing_image(self):
        result, calls = self.run_runtime_fake(missing_resources=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("up" in item["args"] for item in calls))

    def test_runtime_bind_mount_rejected_before_start(self):
        run, calls = self.run_runtime_fake(bad_bind=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any("stop" in item["args"] or "up" in item["args"] for item in calls))
        self.assertIn("confinement", run.stderr)

    def test_rendered_runtime_cannot_reintroduce_capabilities(self):
        result, calls = self.run_runtime_fake(bad_cap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confinement", result.stderr)
        self.assertFalse(any("up" in entry["args"] or "stop" in entry["args"] for entry in calls))

    def test_rendered_runtime_requires_no_new_privileges(self):
        result, calls = self.run_runtime_fake(bad_privileges=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confinement", result.stderr)
        self.assertFalse(any("up" in entry["args"] or "stop" in entry["args"] for entry in calls))

    def test_rendered_runtime_must_join_both_selected_networks(self):
        result, calls = self.run_runtime_fake(bad_runtime_network=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confinement", result.stderr)
        self.assertFalse(any("up" in entry["args"] or "stop" in entry["args"] for entry in calls))

    def test_uncertain_teardown_is_reported_as_failure(self):
        completed, calls = self.run_fake(fail_down=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Cleanup unconfirmed", completed.stderr)
        self.assertTrue(any("down" in item["args"] for item in calls))


if __name__ == "__main__":
    unittest.main()
