"""Behavioral contract for the operation-owned Docker client; never contact a daemon."""
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
from tools.containers import client, operator

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
                "entrypoint": ["python"],
                "command": ["-I", "-m", "conix.worker", "advance"],
                "cpus": 1, "pids_limit": 128,
                "mem_limit": 536870912, "memswap_limit": 536870912,
                "logging": {"driver": "local", "options": {
                    "max-size": "10m", "max-file": "3",
                }},
                "networks": {"database": None, "rpc": None},
                "volumes": [{"source": named_volume, "type": "volume",
                             "target": "/run/conix", "read_only": True}]}
        if os.getenv("CONIX_FAKE_LEGACY_RUNTIME_COMMAND"):
            item["command"] = ["run", "--config", "/run/conix/config.toml"]
        if name := os.getenv("CONIX_FAKE_MISSING_RUNTIME_LIMIT"):
            item.pop(name)
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
            "anvil": {"image": os.environ["CONIX_FOUNDRY_IMAGE"], "user": "65532:65532",
                      "read_only": True, "networks": ["tests"]},
            "initializer": {"image": os.environ["CONIX_TEST_RUNNER_IMAGE"],
                            "read_only": True, "user": "65532:65532",
                            "environment": {"CONIX_TEST_OWNER_DSN": "private-fixture-owner",
                                            "CONIX_TEST_ANVIL_OWNER_DSN": "private-anvil-owner"}}},
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
elif "ps" in args and "compose" not in args:
    if "--format" in args:
        print("0123456789ab")
elif "container" in args and "compose" not in args:
    action = args[args.index("container") + 1]
    if action not in ("stop", "rm"):
        print("Unknown container mutation.", file=sys.stderr)
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
    """Exercise Docker admission and teardown without reaching any daemon."""

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

    def test_retired_owner_dsn_selector_is_rejected_before_docker(self):
        """Schema-owner credentials have no general-purpose container CLI flag."""
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "docker"
            fake.write_text(FAKE, encoding="utf-8")
            fake.chmod(0o755)
            log = Path(directory) / "docker.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = directory + os.pathsep + environment["PATH"]
            environment["CONIX_FAKE_LOG"] = str(log)

            completed = subprocess.run(
                [
                    sys.executable, "-m", "tools.containers.operator",
                    "--context", "science", "--origin", "command line",
                    "image/indexer/check", "--image", "sha256:" + "a" * 64,
                    "--owner-dsn-file", "/tmp/retired-owner-credential",
                ],
                cwd=ROOT, env=environment,
                capture_output=True, text=True, timeout=10,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unrecognized arguments: --owner-dsn-file", completed.stderr)
            self.assertFalse(log.exists(), "Retired credentials reached Docker selection.")

    def test_public_acquisition_worker_cannot_echo_private_stderr(self):
        """A privileged network worker's stderr remains an untrusted string."""
        from types import SimpleNamespace

        credential = "0x" + "ab" * 32
        response = SimpleNamespace(
            returncode=17,
            stdout="",
            stderr=(
                f"Shared acquisition rejected privateKey={credential} on upstream.\n"
                "Shared acquisition failed after a refused response.\n"
            ),
        )
        with patch.object(client.subprocess, "run", return_value=response):
            with self.assertRaises(client.OperationError) as raised:
                client.command(
                    ["docker", "run", "checked-acquisition-worker"],
                    env={}, working_dir=ROOT,
                    public_diagnostic=True,
                )

        explanation = str(raised.exception)
        self.assertIn("exit 17", explanation)
        self.assertIn("Shared acquisition failed after a refused response", explanation)
        self.assertNotIn("privateKey", explanation)
        self.assertNotIn(credential, explanation)

    def test_staging_diagnostics_admit_fixed_causes_but_not_secrets(self):
        admitted = operator._staging_failure(
            b"Conix input staging failed (InputError): Staged inputs were truncated or have trailing bytes.\n"
        )
        self.assertIn("truncated", admitted)
        self.assertIsNone(operator._staging_failure(
            b"Conix input staging failed (InputError): password=0x"
            + b"ab" * 32 + b"\n"
        ))
        self.assertIsNone(operator._staging_failure(
            b"database password in upstream stderr only\n"
        ))

    def test_staging_snapshot_reads_only_declared_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            configuration = b'[contracts.a]\nabi_file="abi/shared.json"\n[contracts.b]\nabi_file="abi/shared.json"\n'
            config.write_bytes(configuration)
            config.chmod(0o600)
            abi = root / "abi"
            abi.mkdir()
            member = abi / "shared.json"
            member.write_bytes(b"[ ]\n")
            member.chmod(0o600)
            (abi / "unused.json").symlink_to(root / "unused.json")
            docker = operator.Docker("unix:///tmp/unused.sock", root)
            self.assertEqual(docker.catalog_snapshot(config), (configuration, {"shared.json": b"[ ]\n"}))

            member.unlink()
            member.symlink_to(config)
            with self.assertRaises(operator.OperationError):
                docker.catalog_snapshot(config)

    def test_staging_snapshot_refuses_paths_outside_its_flat_abi_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            docker = operator.Docker("unix:///tmp/unused.sock", root)
            for relative in ("../other.json", "abi/../other.json", "abi/nested/other.json", "/other.json"):
                with self.subTest(relative=relative), self.assertRaises(operator.OperationError) as failure:
                    config.write_text(f'[contracts.a]\nabi_file="{relative}"\n')
                    config.chmod(0o600)
                    docker.catalog_snapshot(config)
                self.assertIn("ABI paths", str(failure.exception))

    def test_staging_timeout_stays_primary_when_volume_cleanup_is_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = operator.Docker("unix:///tmp/unused.sock", Path(directory))
            token = "a" * 24
            volume = "conix-inputs-" + token
            private_value = "do-not-print-private-input"
            timeout = subprocess.TimeoutExpired(["docker", private_value], 40)
            with (
                patch.object(operator.secrets, "token_hex", return_value=token),
                patch.object(operator.subprocess, "run", side_effect=timeout),
                patch.object(docker, "require_role"),
                patch.object(docker, "catalog_snapshot", return_value=(b"settings", {"a.json": b"[]"})),
                patch.object(docker, "private_bytes", return_value=private_value.encode()),
                patch.object(docker, "run", return_value=volume),
                patch.object(docker, "remove_inputs", side_effect=operator.OperationError("cleanup failed")) as remove,
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
                self.assertRaises(operator.OperationError) as failure,
            ):
                docker.stage_inputs("sha256:" + "b" * 64, "/config.toml", "/dsn")

            self.assertIs(failure.exception.__cause__, timeout)
            self.assertIn("timed out", str(failure.exception))
            self.assertIn(volume, stderr.getvalue())
            self.assertNotIn(private_value, str(failure.exception) + stderr.getvalue())
            remove.assert_called_once_with(volume, token=token)

    def test_uncertain_volume_creation_reports_the_selected_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = operator.Docker("unix:///tmp/unused.sock", Path(directory))
            token = "a" * 24
            creation_failure = operator.OperationError("Docker volume create timed out.")
            with (
                patch.object(operator.secrets, "token_hex", return_value=token),
                patch.object(docker, "require_role"),
                patch.object(docker, "catalog_snapshot", return_value=(b"settings", {"a.json": b"[]"})),
                patch.object(docker, "private_bytes", return_value=b"credential"),
                patch.object(docker, "run", side_effect=creation_failure),
                patch.object(docker, "remove_inputs") as remove,
                self.assertRaises(operator.OperationError) as failure,
            ):
                docker.stage_inputs("sha256:" + "b" * 64, "/config.toml", "/dsn")

            self.assertIs(failure.exception.__cause__, creation_failure)
            self.assertIn("conix-inputs-" + token, str(failure.exception))
            self.assertIn(docker.endpoint, str(failure.exception))
            remove.assert_not_called()

    def test_docker_client_errors_retain_cause_without_private_arguments(self):
        private_argument = "postgresql://private:password@secret.example"
        with patch.object(
            operator.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["docker", private_argument], 5),
        ):
            with self.assertRaises(operator.OperationError) as timed_out:
                client.command(["docker", "run", private_argument], env={}, working_dir=ROOT, timeout=5)

        self.assertIn("timed out", str(timed_out.exception))
        self.assertIn("unconfirmed", str(timed_out.exception))
        self.assertNotIn(private_argument, str(timed_out.exception))

        with patch.object(
            operator.subprocess,
            "run",
            side_effect=PermissionError(errno.EACCES, "denied"),
        ):
            with self.assertRaises(operator.OperationError) as refused:
                client.command(["docker", "run", private_argument], env={}, working_dir=ROOT)

        self.assertIn("PermissionError", str(refused.exception))
        self.assertIn(str(errno.EACCES), str(refused.exception))
        self.assertNotIn(private_argument, str(refused.exception))

    def test_terminal_permission_error_is_not_claimed_to_mean_no_terminal(self):
        with patch.object(
            client, "command", return_value="science\tunix:///tmp/science.sock\n"
        ), patch("builtins.open", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(operator.OperationError) as refused:
                client.select_endpoint("", "undefined", working_dir=ROOT)

        self.assertIn("PermissionError", str(refused.exception))
        self.assertNotIn("No terminal", str(refused.exception))

    def test_endpoint_only_accepts_local_unix_socket(self):
        for value in ("unix:///run/docker.sock", "unix:///tmp/other.sock"):
            self.assertTrue(client.local_socket(value))
        for value in ("tcp://localhost:2375", "ssh://host", "unix://relative",
                      "unix:///", "unix:///run/docker.sock?query=1"):
            self.assertFalse(client.local_socket(value))

    def test_host_root_is_refused_before_engine_discovery(self):
        with patch.object(client.os, "geteuid", return_value=0), patch.object(
            client, "command", side_effect=AssertionError("Docker contacted before host identity admission")
        ):
            with self.assertRaises(operator.OperationError) as error:
                client.select_endpoint("science", "command line", working_dir=ROOT)
        self.assertIn("root", str(error.exception).lower())

    def test_invalid_or_ambient_context_does_not_choose_an_engine(self):
        with patch.object(client, "command", return_value="science\tunix:///tmp/science.sock\n"):
            with self.assertRaises(operator.OperationError):
                client.select_endpoint("unknown", "command line", working_dir=ROOT)
            with patch("builtins.open", side_effect=OSError):
                with self.assertRaises(operator.OperationError):
                    client.select_endpoint("science", "environment", working_dir=ROOT)

    def run_fake(self, *, fail_up=False, fail_down=False, bad_network=False, role="test-runner"):
        """Use a recording Docker executable that refuses unrecognized effects."""
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
            env["CONIX_FAKE_ROLE"] = role
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

    def test_wrong_role_image_fails_before_database_or_runtime_side_effects(self):
        pg, sql_calls = self.run_fake(role="indexer")
        self.assertNotEqual(pg.returncode, 0)
        self.assertFalse(any("up" in x["args"] or "down" in x["args"] for x in sql_calls))

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

    def run_runtime_fake(self, *, bad_bind=False, operation="config/check", missing_resources=False,
                         role="indexer",
                         bad_cap=False, bad_privileges=False, bad_runtime_network=False,
                         legacy_command=False, missing_limit=None,
                         rootful=False, fail_up=False):
        """Prove deployment confinement before start and recovery after failure."""
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fake = directory / "docker"
            fake.write_text(FAKE)
            fake.chmod(0o755)
            log = directory / "docker.jsonl"
            env = os.environ.copy()
            env["PATH"] = str(directory) + os.pathsep + env["PATH"]
            env["CONIX_FAKE_LOG"] = str(log)
            env["CONIX_FAKE_ROLE"] = role
            env["DOCKER_HOST"] = "tcp://malicious.invalid:2375"
            env["COMPOSE_FILE"] = "/tmp/foreign.yml"
            if legacy_command:
                env["CONIX_FAKE_LEGACY_RUNTIME_COMMAND"] = "1"
            if missing_limit:
                env["CONIX_FAKE_MISSING_RUNTIME_LIMIT"] = missing_limit
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
            if fail_up:
                env["CONIX_FAKE_UP_FAIL"] = "1"
            result = subprocess.run([
                sys.executable, "-m", "tools.containers.operator",
                "--context", "science", "--origin", "command line",
                "deployment/" + operation, "--deployment", "pilot",
                "--indexer-image", "sha256:" + "a" * 64,
                "--volume", "conix-inputs-" + "b" * 24,
                "--database-network", "db-conix", "--rpc-network", "rpc-conix",
            ], env=env, capture_output=True, text=True, timeout=15)
            return result, [
                json.loads(line) for line in log.read_text().splitlines()
            ] if log.exists() else []

    def test_runtime_config_checks_containment_without_starting_obsolete_worker(self):
        run, calls = self.run_runtime_fake()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(any("config" in record["args"] for record in calls))
        self.assertFalse(any(
            "up" in record["args"] or "stop" in record["args"]
            for record in calls
        ))
        for record in calls:
            if "context" not in record["args"]:
                self.assertIn("unix:///tmp/science-docker.sock", record["args"])

    def test_runtime_configuration_rejects_deleted_cli_entrypoint(self):
        """A safe container with an obsolete worker command is not deployable."""
        result, calls = self.run_runtime_fake(legacy_command=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confinement", result.stderr)
        self.assertFalse(any("up" in call["args"] for call in calls))

    def test_runtime_stop_does_not_need_removed_external_resources(self):
        result, calls = self.run_runtime_fake(operation="remove", missing_resources=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("container" in item["args"] and "stop" in item["args"] for item in calls))
        self.assertTrue(any("container" in item["args"] and "rm" in item["args"] for item in calls))
        self.assertFalse(any("compose" in item["args"] for item in calls))
        self.assertFalse(any("image" in item["args"] and "inspect" in item["args"] for item in calls))
        self.assertFalse(any("volume" in item["args"] and "inspect" in item["args"] for item in calls))

    def test_retired_deployment_apply_action_cannot_start_a_container(self):
        result, calls = self.run_runtime_fake(operation="apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(
            "up" in item["args"] or "stop" in item["args"] for item in calls
        ))

    def test_runtime_status_does_not_require_absent_image_or_inputs(self):
        result, calls = self.run_runtime_fake(operation="status", missing_resources=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("image" in x["args"] and "inspect" in x["args"] for x in calls))
        self.assertFalse(any("volume" in x["args"] and "inspect" in x["args"] for x in calls))

    def test_recovery_does_not_require_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "docker"
            fake.write_text(FAKE)
            fake.chmod(0o755)
            log = Path(folder) / "docker.jsonl"
            env = os.environ.copy()
            env["PATH"] = folder + os.pathsep + env["PATH"]
            env["CONIX_FAKE_LOG"] = str(log)

            for action in ("status", "remove"):
                with self.subTest(action=action):
                    result = subprocess.run([
                        sys.executable, "-m", "tools.containers.operator",
                        "--context", "science", "--origin", "command line",
                        "deployment/" + action, "--deployment", "pilot",
                    ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)

            arguments = [json.loads(line)["args"] for line in log.read_text().splitlines()]
            self.assertFalse(any("compose" in args for args in arguments))
            self.assertFalse(any("up" in args for args in arguments))
            self.assertFalse(any("image" in args and "inspect" in args for args in arguments))

    def test_runtime_bind_mount_rejected_before_start(self):
        run, calls = self.run_runtime_fake(bad_bind=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any("stop" in item["args"] or "up" in item["args"] for item in calls))
        self.assertIn("confinement", run.stderr)

    def test_prospective_runtime_requires_declared_resource_limits(self):
        """Each independent resource boundary is mandatory before activation."""
        for limit in ("cpus", "pids_limit", "mem_limit", "memswap_limit", "logging"):
            with self.subTest(limit=limit):
                result, calls = self.run_runtime_fake(missing_limit=limit)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("confinement", result.stderr)
                self.assertFalse(any("up" in call["args"] for call in calls))

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


    def test_primary_test_error_survives_independent_cleanup_failure(self):
        """The original test error stays primary if project removal fails."""
        cleanup = operator.OperationError("Compose project retirement failed.")
        for original in (
            operator.OperationError("Original test execution failed."),
            KeyboardInterrupt(),
        ):
            with self.subTest(primary=type(original).__name__):
                self._verify_primary_test_failure_survives_cleanup(original, cleanup)

    def _verify_primary_test_failure_survives_cleanup(self, original, cleanup):
        with (
            patch.object(operator.Docker, "require_role"),
            patch.object(operator.Docker, "require_image"),
            patch.object(operator.Docker, "run") as invoke,
            patch("sys.stderr", new_callable=io.StringIO) as messages,
        ):
            runtime = operator.Docker("unix:///tmp/selected.sock", ROOT)

            # Keep real graph admission: the recording Docker model provides
            # a reviewed fixture instead of bypassing the checks.
            admitted = {
                "services": {
                    "tests": {"image": "sha256:" + "a" * 64, "user": "65532:65532", "read_only": True},
                    "initializer": {"image": "sha256:" + "a" * 64, "user": "65532:65532", "read_only": True,
                                    "environment": {"CONIX_TEST_OWNER_DSN": "fixture",
                                                    "CONIX_TEST_ANVIL_OWNER_DSN": "fixture"}},
                    "postgres": {"image": "postgres", "user": "999:999", "read_only": True},
                    "bootstrap": {"image": "postgres", "user": "999:999", "read_only": True},
                    "anvil": {"image": "foundry", "user": "65532:65532", "read_only": True,
                              "networks": ["tests"]},
                },
                "networks": {"tests": {"internal": True}},
            }

            def run(*argv, **_options):
                if "config" in argv:
                    return json.dumps(admitted)
                if "up" in argv:
                    raise original
                if "down" in argv:
                    raise cleanup
                return ""

            invoke.side_effect = run
            with self.assertRaises(type(original)) as raised:
                runtime.test("sha256:" + "a" * 64, {"postgres": "postgres", "foundry": "foundry"})

        self.assertIs(raised.exception, original)
        self.assertTrue(any("down" in call.args for call in invoke.call_args_list))
        self.assertIn("Cleanup unconfirmed", messages.getvalue())

    def test_uncertain_teardown_is_reported_as_failure(self):
        completed, calls = self.run_fake(fail_down=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Cleanup unconfirmed", completed.stderr)
        self.assertTrue(any("down" in item["args"] for item in calls))


if __name__ == "__main__":
    unittest.main()
