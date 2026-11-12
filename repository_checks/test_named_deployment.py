"""Named lifecycle selects settings once and never puts secrets into Docker argv."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import stat
import tempfile
import unittest

from tools.containers.client import OperationError
from tools.containers import named


IMAGE = "sha256:" + "a" * 64
VOLUME = "conix-inputs-" + "b" * 24


class RecordingDocker:
    seen = []

    def __init__(self, endpoint, config_dir):
        self.endpoint = endpoint
        self.config_dir = config_dir
        self.calls = []
        self.docker_command_prefix = ["docker", "--host", endpoint]
        self.docker_environment = {}
        type(self).seen.append(self)

    def run(self, *args, **_kw):
        self.calls.append((args[0], args[1] if len(args) > 1 else ""))
        if args[0] == "ps":
            return ""
        raise AssertionError(f"Unexpected low-level operation {args[0]}")

    def require_rootless_engine(self):
        self.calls.append(("rootless", ""))

    def require_role(self, image, role):
        self.calls.append(("role", role))
        assert (image, role) == (IMAGE, "indexer")

    def check_indexer(self, image):
        assert image == IMAGE

    def stage_inputs(self, image, source, dsn, *, announce):
        self.calls.append(("staging", dsn))
        assert not announce
        return VOLUME

    def private_bytes(self, *_args, **_kwargs):
        return b"postgresql://owner:secret@postgres:5432/db\n"

    def verify_inputs(self, image, volume):
        assert (image, volume) == (IMAGE, VOLUME)

    def deployment(self, *, operation, name, image, volume, database, rpc):
        self.calls.append((operation, name))
        if operation in ("start", "config/check"):
            assert (image, volume, database, rpc) == (IMAGE, VOLUME, "db-conix", "rpc-conix")


class NamedDeploymentContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.patch_root = patch.multiple(
            named, ROOT=base, DEPLOYMENTS=base / "config" / "deployments",
        )
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.patch_endpoint = patch.object(
            named, "select_endpoint", return_value="unix:///tmp/test-engine.sock",
        )
        self.patch_endpoint.start()
        self.addCleanup(self.patch_endpoint.stop)
        self.options = SimpleNamespace(
            origin="command line", deployment="pilot", context="science",
            config_file="/project/source.toml",
            dsn_file="/project/private/runtime.dsn",
            owner_dsn_file="/project/private/owner.dsn",
            database_network="db-conix", rpc_network="rpc-conix",
            wheelhouse="/shared/wheels",
        )
        RecordingDocker.seen = []

    def configure(self):
        named.init("pilot", self.options)
        return named._directory("pilot")

    def prepared(self):
        folder = self.configure()
        named._write_prepared(folder, {"version": 1, "image": IMAGE, "volume": VOLUME})
        self.options.context = ""
        self.options.origin = "undefined"
        return folder

    def execute(self, action):
        return named.dispatch(
            action, self.options, client_cls=RecordingDocker,
            pinned_images=lambda: {"python_base": "reviewed"},
        )

    def test_init_records_only_configuration_and_refuses_overwrite(self):
        folder = self.configure()
        declaration = folder / "deployment.toml"
        self.assertEqual(stat.S_IMODE(declaration.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(named._read("pilot").database_network, "db-conix")
        self.assertNotIn("postgresql://", declaration.read_text())
        with self.assertRaises(OperationError):
            self.configure()
        self.assertFalse(RecordingDocker.seen, "Initialization must not select an Engine.")

    def test_status_and_down_use_saved_engine_without_image_or_volume(self):
        self.configure()
        self.options.context = ""
        self.options.origin = "undefined"
        self.execute("deployment/status")
        self.execute("deployment/down")
        self.assertEqual(
            [d.calls[-1][0] for d in RecordingDocker.seen],
            ["status", "remove"],
        )
        self.assertTrue(all(d.endpoint == "unix:///tmp/test-engine.sock" for d in RecordingDocker.seen))

    def test_conflicting_context_and_corrupted_settings_fail_before_docker(self):
        folder = self.configure()
        self.options.context = "different"
        with self.assertRaises(OperationError):
            self.execute("deployment/status")
        self.assertFalse(RecordingDocker.seen)
        self.options.context = ""
        self.options.origin = "undefined"
        (folder / "deployment.toml").chmod(0o644)
        with self.assertRaises(OperationError):
            self.execute("deployment/status")
        self.assertFalse(RecordingDocker.seen)

    def test_up_requires_prepared_artifacts_but_status_does_not(self):
        self.configure()
        self.options.context = ""
        self.options.origin = "undefined"
        with self.assertRaises(OperationError):
            self.execute("deployment/up")
        self.execute("deployment/status")
        self.assertEqual(RecordingDocker.seen[-1].calls[-1][0], "status")

    def test_up_uses_exact_saved_prepared_image_and_volume(self):
        self.prepared()
        self.execute("deployment/config/check")
        self.execute("deployment/up")
        self.assertEqual(
            [d.calls[-1][0] for d in RecordingDocker.seen],
            ["config/check", "start"],
        )

    def test_prepare_reuses_checked_build_and_stages_once_without_rootless_start(self):
        self.configure()
        self.options.context = ""
        self.options.origin = "undefined"
        with patch("tools.wheelhouse.operations.SharedWheelhouse") as wheelhouse:
            selected = wheelhouse.return_value
            selected.build.return_value = IMAGE
            self.execute("deployment/prepare")

        selected.check.assert_called_once_with()
        selected.build.assert_called_once_with("indexer")
        self.assertEqual(named._prepared(named._read("pilot")), {
            "version": 1, "image": IMAGE, "volume": VOLUME,
        })
        self.assertIn(("staging", "/project/private/runtime.dsn"), RecordingDocker.seen[-1].calls)
        self.assertNotIn(("rootless", ""), RecordingDocker.seen[-1].calls)

    def test_database_initializer_uses_owner_stdin_only(self):
        self.prepared()
        from types import SimpleNamespace
        calls = []
        def run(argv, *, input, **_kwargs):
            calls.append((argv, input))
            return SimpleNamespace(returncode=0)
        with (
            patch.object(named, "_admit_volume"),
            patch.object(named, "_admit_network", return_value="d" * 64),
            patch.object(named.subprocess, "run", side_effect=run),
        ):
            self.execute("deployment/database/init")
        self.assertEqual(len(calls), 1)
        argv, payload = calls[0]
        self.assertEqual(payload, b"postgresql://owner:secret@postgres:5432/db\n")
        self.assertNotIn("postgresql://", " ".join(argv))
        self.assertIn("conix.schema_cli", argv)
        self.assertIn("--network", argv)
        self.assertNotIn("owner.dsn", " ".join(argv))

    def test_init_never_guesses_a_missing_network(self):
        self.options.rpc_network = ""
        with patch.object(named, "_prompt", side_effect=OperationError("explicit RPC network required")):
            with self.assertRaises(OperationError):
                named.init("pilot", self.options)
        self.assertFalse(named._directory("pilot").exists())
