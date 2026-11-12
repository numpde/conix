"""Behavioral contracts for self-contained named deployment configuration.

These tests deliberately exercise the real host selection/projection and
prepared lock boundaries. RecordingDocker does not substitute for contained
catalog tests or an actual retained-rootless lifecycle qualification.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.containers import named
from tools.containers.client import OperationError


IMAGE = "sha256:" + "a" * 64
VOLUME = "conix-inputs-" + "b" * 24
VOLUME_NEW = "conix-inputs-" + "c" * 24
OWNER = b"postgresql://owner:private@postgres:5432/conix_test\n"

SOURCE = {
    "version": 1,
    "docker_context": "science",
    "wheelhouse": "/shared/wheels",
    "database_network": "db-conix",
    "rpc_network": "rpc-conix",
    "chain": {
        "chain_id": 31337,
        "genesis_hash": "0x" + "1" * 64,
        "start_block": 1,
        "rpc_url": "http://anvil:8545",
    },
    "contracts": {
        "token": {
            "address": "0x" + "2" * 40,
            "abi_file": "abi/token.json",
            "events": ["Transfer(address,address,uint256)"],
        },
    },
    "limits": {
        "rpc_timeout_seconds": 5,
        "operation_timeout_seconds": 30,
        "max_response_bytes": 1048576,
        "max_connections": 4,
        "max_page_size": 100,
        "max_evidence_blocks": 64,
        "max_logs_per_block": 10000,
        "max_abi_bytes": 1048576,
        "max_abi_nesting": 8,
        "max_abi_elements": 4096,
    },
}


class RecordingDocker:
    seen = []

    def __init__(self, endpoint, config_dir):
        self.endpoint = endpoint
        self.config_dir = config_dir
        self.calls = []
        self.stage_failure = False
        self.volume = VOLUME
        self.projected = None
        self.docker_command_prefix = ["docker", "--host", endpoint]
        self.docker_environment = {}
        type(self).seen.append(self)

    def run(self, *args, **_kwargs):
        self.calls.append(args)
        if args[0] == "ps":
            return ""
        raise AssertionError(f"Unexpected low-level operation {args[0]}")

    def require_rootless_engine(self):
        self.calls.append(("rootless",))

    def require_role(self, image, role):
        self.calls.append(("role", role))
        assert (image, role) == (IMAGE, "indexer")

    def check_indexer(self, image):
        self.calls.append(("check_image", image))
        assert image == IMAGE

    def stage_inputs(self, image, config_path, dsn_path, *, announce):
        assert not announce
        self.calls.append(("stage", image, dsn_path))
        self.projected = Path(config_path).read_text()
        self.projected_path = Path(config_path)
        if self.stage_failure:
            raise OperationError("Staging failed before publication.")
        return self.volume

    def remove_inputs(self, volume):
        self.calls.append(("remove_volume", volume))

    def private_bytes(self, path, max_size, *, secret):
        self.calls.append(("private_bytes", path, max_size, secret))
        return OWNER

    def verify_inputs(self, image, volume):
        self.calls.append(("verify_inputs", image, volume))

    def deployment(self, *, operation, name, image, volume, database, rpc):
        self.calls.append(("deployment", operation, name, image, volume, database, rpc))


class NamedDeploymentContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        p = patch.multiple(named, ROOT=base, DEPLOYMENTS=base / "config" / "deployments")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(named, "select_endpoint", return_value="unix:///tmp/science.sock")
        p.start()
        self.addCleanup(p.stop)
        RecordingDocker.seen = []
        self.options = SimpleNamespace(
            origin="command line",
            deployment="pilot", context="science",
            config_file="", dsn_file="", owner_dsn_file="",
            database_network="", rpc_network="", wheelhouse="",
        )

    def folder(self):
        return named._directory("pilot")

    def init(self):
        named.init("pilot", self.options)
        return self.folder()

    def configured(self):
        folder = self.init()
        # Populate ONLY the editable deployment TOML with a source selection.
        root = SOURCE.copy()
        body = named._render_deployment(root)
        (folder / "deployment.toml").write_text(body, encoding="utf-8")
        (folder / "deployment.toml").chmod(0o600)
        (folder / "abi" / "token.json").write_text("[]\n")
        (folder / "abi" / "token.json").chmod(0o600)
        for name in ("runtime.dsn", "owner.dsn"):
            path = folder / "secrets" / name
            path.write_bytes(OWNER if name == "owner.dsn" else b"postgresql://runtime@postgres/db\n")
            path.chmod(0o600)
        return folder

    def locked(self, record=None):
        folder = self.configured()
        named._write_lock(folder, record or {"version": 1, "image": IMAGE, "volume": VOLUME})
        self.options.context = ""
        self.options.origin = "undefined"
        return folder

    def dispatch(self, action):
        return named.dispatch(
            action, self.options,
            client_cls=RecordingDocker, pinned_images=lambda: {"python_base": "reviewed"},
        )

    def test_init_creates_skeleton_and_owned_directories_without_inventing_source(self):
        folder = self.init()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for subdir in ("abi", "secrets"):
            self.assertEqual(stat.S_IMODE((folder / subdir).stat().st_mode), 0o700)
            self.assertEqual(list((folder / subdir).iterdir()), [])
        config = folder / "deployment.toml"
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        data = tomllib.loads(config.read_text())
        self.assertEqual(data["docker_context"], "science")
        self.assertNotIn("chain_id", str(data))
        self.assertNotIn("genesis_hash", data)
        self.assertNotIn("source_config", data)
        self.assertNotIn("runtime_dsn_file", data)
        self.assertNotIn("owner_dsn_file", data)
        self.assertFalse((folder / "deployment.lock.toml").exists())
        self.assertFalse((folder / "prepared.json").exists())
        self.assertFalse(RecordingDocker.seen)
        with self.assertRaises(OperationError):
            self.init()

    def test_projection_uses_authoritative_catalog_shape_and_derived_paths(self):
        folder = self.configured()
        decl = named._read("pilot")
        projected = tomllib.loads(named._project_catalog(decl))
        self.assertEqual(set(projected), {"version", "chain", "contracts", "database", "limits"})
        self.assertEqual(projected["chain"], SOURCE["chain"])
        self.assertEqual(projected["contracts"], SOURCE["contracts"])
        self.assertEqual(projected["limits"], SOURCE["limits"])
        self.assertEqual(projected["database"], {
            "dsn_file": "/run/conix/connection.dsn", "runtime_role": "conix_runtime",
        })
        self.assertEqual(decl.runtime_dsn_file, folder / "secrets" / "runtime.dsn")
        self.assertEqual(decl.owner_dsn_file, folder / "secrets" / "owner.dsn")
        self.assertEqual(decl.abi_dir, folder / "abi")
        self.assertEqual(decl.lock_file, folder / "deployment.lock.toml")

    def test_missing_selections_are_reported_without_implicit_defaults(self):
        self.init()
        with self.assertRaisesRegex(OperationError, "wheelhouse"):
            self.dispatch("deployment/prepare")
        self.assertFalse(RecordingDocker.seen)

    def test_no_old_path_based_deployment_settings_are_accepted(self):
        folder = self.configured()
        source = (folder / "deployment.toml").read_text()
        for key in ("source_config", "runtime_dsn_file", "owner_dsn_file"):
            with self.subTest(key=key):
                (folder / "deployment.toml").write_text(
                    source + f'\n{key} = "/untrusted/path"\n'
                )
                with self.assertRaises(OperationError):
                    named._read("pilot")
        (folder / "deployment.toml").write_text(source)

    def test_prepare_projects_once_and_stages_runtime_only(self):
        folder = self.configured()
        self.options.origin = "undefined"
        self.options.context = ""
        with patch("tools.wheelhouse.operations.SharedWheelhouse") as wheelhouse:
            wheelhouse.return_value.build.return_value = IMAGE
            self.dispatch("deployment/prepare")
            wheelhouse.return_value.check.assert_called_once()
            wheelhouse.return_value.build.assert_called_once_with("indexer")

        docker = RecordingDocker.seen[-1]
        self.assertEqual(docker.calls[-1][0], "stage")
        self.assertEqual(docker.calls[-1][2], folder / "secrets" / "runtime.dsn")
        self.assertEqual(tomllib.loads(docker.projected)["chain"], SOURCE["chain"])
        self.assertNotIn("owner", docker.projected.lower())
        self.assertFalse(docker.projected_path.exists(), "Temporary projected catalog must retire.")
        self.assertFalse(any(call[0] == "private_bytes" for call in docker.calls))
        self.assertEqual(named._lock(named._read("pilot")), {
            "version": 1, "image": IMAGE, "volume": VOLUME,
        })
        self.assertFalse((folder / "prepared.json").exists())
        self.assertEqual(stat.S_IMODE((folder / "deployment.lock.toml").stat().st_mode), 0o600)

    def test_failed_staging_does_not_replace_previous_lock(self):
        self.locked()
        old = (self.folder() / "deployment.lock.toml").read_bytes()
        self.options.context = ""
        with patch("tools.wheelhouse.operations.SharedWheelhouse") as wheelhouse:
            wheelhouse.return_value.build.return_value = IMAGE
            # A failure before writing the lock cannot touch old resources.
            original = RecordingDocker.stage_inputs
            def fail(docker, *args, **kwargs):
                docker.stage_failure = True
                return original(docker, *args, **kwargs)
            with patch.object(RecordingDocker, "stage_inputs", fail):
                with self.assertRaises(OperationError):
                    self.dispatch("deployment/prepare")
        self.assertEqual((self.folder() / "deployment.lock.toml").read_bytes(), old)
        self.assertFalse(any(call[0] == "remove_volume" for call in RecordingDocker.seen[-1].calls))

    def test_failed_lock_publication_preserves_old_and_retires_new_volume(self):
        self.locked()
        old = (self.folder() / "deployment.lock.toml").read_bytes()
        self.options.context = ""
        with (
            patch("tools.wheelhouse.operations.SharedWheelhouse") as wheelhouse,
            patch.object(named, "_write_lock", side_effect=OSError("disk unavailable")),
        ):
            wheelhouse.return_value.build.return_value = IMAGE
            RecordingDocker.seen = []
            with self.assertRaises(OSError):
                self.dispatch("deployment/prepare")
        self.assertEqual((self.folder() / "deployment.lock.toml").read_bytes(), old)
        self.assertIn(("remove_volume", VOLUME), RecordingDocker.seen[-1].calls)

    def test_successful_replacement_changes_lock_atomically_then_retires_old_volume(self):
        self.locked({"version": 1, "image": IMAGE, "volume": VOLUME})
        with patch("tools.wheelhouse.operations.SharedWheelhouse") as wheelhouse:
            wheelhouse.return_value.build.return_value = IMAGE
            original = RecordingDocker.stage_inputs
            def new_volume(docker, *args, **kwargs):
                docker.volume = VOLUME_NEW
                return original(docker, *args, **kwargs)
            with patch.object(RecordingDocker, "stage_inputs", new_volume):
                self.dispatch("deployment/prepare")
        self.assertEqual(named._lock(named._read("pilot"))["volume"], VOLUME_NEW)
        self.assertIn(("remove_volume", VOLUME), RecordingDocker.seen[-1].calls)
        self.assertFalse(any(p.name.startswith(".prepared-") for p in self.folder().iterdir()))

    def test_lock_admission_rejects_invalid_toml_and_legacy_json(self):
        folder = self.configured()
        (folder / "prepared.json").write_text(json.dumps({
            "version": 1, "image": IMAGE, "volume": VOLUME,
        }))
        with self.assertRaises(OperationError):
            self.dispatch("deployment/up")
        self.assertFalse(RecordingDocker.seen, "Legacy JSON is never accepted.")
        for payload in ("not = [", "version = true\nimage = " + json.dumps(IMAGE)
                        + "\nvolume = " + json.dumps(VOLUME),
                        "version = 1\nimage = " + json.dumps(IMAGE)
                        + "\nvolume = " + json.dumps(VOLUME) + "\nextra = 1"):
            with self.subTest(payload=payload):
                lock = folder / "deployment.lock.toml"
                lock.write_text(payload)
                lock.chmod(0o600)
                with self.assertRaises(OperationError):
                    self.dispatch("deployment/up")

    def test_actual_rename_failure_preserves_previous_toml_and_cleans_temporary(self):
        folder = self.locked()
        lock = folder / "deployment.lock.toml"
        before = lock.read_bytes()
        with patch.object(named.os, "replace", side_effect=OSError("atomic rename unavailable")):
            with self.assertRaises(OSError):
                named._write_lock(folder, {
                    "version": 1, "image": IMAGE, "volume": VOLUME_NEW,
                })
        self.assertEqual(lock.read_bytes(), before)
        self.assertFalse(list(folder.glob(".deployment-lock-*")))

    def test_named_actions_refuse_legacy_per_operation_resource_overrides(self):
        for key in ("config_file", "dsn_file", "indexer_image", "volume",
                    "database_network", "rpc_network", "wheelhouse"):
            with self.subTest(selector=key):
                self.options.__dict__[key] = "other-resource"
                with self.assertRaisesRegex(OperationError, "deployment.toml"):
                    self.dispatch("deployment/init")
                self.options.__dict__[key] = ""
        self.assertFalse(self.folder().exists())
        self.assertFalse(RecordingDocker.seen)

    def test_up_revalidates_exact_prepared_selection_under_operator_lock(self):
        folder = self.locked()
        inside = [False]
        original = named._lock
        @contextmanager
        def locked(_name):
            inside[0] = True
            try:
                yield
            finally:
                inside[0] = False
        def read_lock(decl):
            self.assertTrue(inside[0])
            return original(decl)
        with patch.object(named, "_locked", locked), patch.object(named, "_lock", read_lock):
            self.dispatch("deployment/up")
        observed = RecordingDocker.seen[-1].calls[-1]
        self.assertEqual(observed, (
            "deployment", "start", "pilot", IMAGE, VOLUME, "db-conix", "rpc-conix",
        ))

    def test_owner_dsn_is_read_only_for_initialization_and_never_mounted(self):
        folder = self.locked()
        with (
            patch.object(named, "_admit_volume"),
            patch.object(named, "_admit_network", return_value="d"*64),
            patch.object(named.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as invoked,
        ):
            self.dispatch("deployment/database/init")
        docker = RecordingDocker.seen[-1]
        self.assertIn(("private_bytes", folder / "secrets" / "owner.dsn", 8192, True), docker.calls)
        argv = invoked.call_args.args[0]
        self.assertNotIn(OWNER.decode("utf-8").strip(), " ".join(argv))
        self.assertNotIn(str(folder), " ".join(argv))
        self.assertEqual(invoked.call_args.kwargs["input"], OWNER)
        self.assertIn("conix.schema_cli", argv)

    def test_status_down_do_not_need_lock_or_source_setup(self):
        self.init()
        self.options.context = ""
        self.options.origin = "undefined"
        self.dispatch("deployment/status")
        self.dispatch("deployment/down")
        self.assertEqual(
            [docker.calls[-1][1] for docker in RecordingDocker.seen],
            ["status", "remove"],
        )

    def test_configuration_permission_and_engine_conflict_still_fail_closed(self):
        folder = self.configured()
        self.options.context = "other"
        with self.assertRaises(OperationError):
            self.dispatch("deployment/up")
        self.assertFalse(RecordingDocker.seen)
        self.options.context = ""
        config = folder / "deployment.toml"
        config.chmod(0o644)
        with self.assertRaises(OperationError):
            self.dispatch("deployment/prepare")
        self.assertFalse(RecordingDocker.seen)

    def test_selected_abi_cannot_escape_private_directory(self):
        folder = self.configured()
        source = (folder / "deployment.toml").read_text()
        with self.subTest("traversal"):
            (folder / "deployment.toml").write_text(
                source.replace("abi/token.json", "../outside.json")
            )
            with self.assertRaises(OperationError):
                named._read("pilot")
        with self.subTest("symlink"):
            (folder / "deployment.toml").write_text(source)
            (folder / "abi" / "token.json").unlink()
            (folder / "abi" / "token.json").symlink_to(folder / "external.json")
            with self.assertRaises(OperationError):
                named._read("pilot")


if __name__ == "__main__":
    unittest.main()
