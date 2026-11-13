"""Named Reader workflow must derive its source without writer authority."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.containers import named
from tools.containers.client import OperationError
from test_named_deployment import (
    IMAGE, OWNER, SOURCE, VOLUME, VOLUME_NEW, RecordingDocker,
)


READER_DSN = b"postgresql://conix_reader:private@postgres:5432/conix_test\n"


class ReaderDocker(RecordingDocker):
    def __init__(self, endpoint, config_dir):
        super().__init__(endpoint, config_dir)
        self.volume = VOLUME_NEW

    def reader_check(self, *, name, image, volume, database, rpc):
        self.calls.append(("reader_check", name, image, volume, database, rpc))
        return {"outcome": "reader_ready", "source_id": "a" * 64, "checkpoint": None}


class NamedReaderContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.patched = patch.multiple(named, ROOT=root, DEPLOYMENTS=root / "config" / "deployments")
        self.patched.start()
        self.addCleanup(self.patched.stop)
        self.endpoint = patch.object(named, "select_endpoint", return_value="unix:///tmp/science.sock")
        self.endpoint.start()
        self.addCleanup(self.endpoint.stop)
        ReaderDocker.seen = []
        self.options = SimpleNamespace(
            origin="undefined", deployment="pilot", context="",
            config_file="", dsn_file="", owner_dsn_file="",
            database_network="", rpc_network="", wheelhouse="",
        )
        self.folder = named._directory("pilot")
        self.folder.mkdir(parents=True, mode=0o700)
        (self.folder / "abi").mkdir(mode=0o700)
        (self.folder / "secrets").mkdir(mode=0o700)
        cfg = self.folder / "deployment.toml"
        cfg.write_text(named._render_deployment(SOURCE))
        cfg.chmod(0o600)
        (self.folder / "abi" / "token.json").write_text("[]\n")
        (self.folder / "abi" / "token.json").chmod(0o600)
        for label, content in (
            ("runtime.dsn", b"postgresql://conix_runtime:writer-only@postgres/conix\n"),
            ("owner.dsn", OWNER),
            ("reader.dsn", READER_DSN),
        ):
            path = self.folder / "secrets" / label
            path.write_bytes(content)
            path.chmod(0o600)
        named._write_lock(self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME})

    def dispatch(self, action):
        return named.dispatch(
            action, self.options,
            client_cls=ReaderDocker,
            pinned_images=lambda: {},
        )

    def test_reader_catalog_is_derived_from_same_selected_source(self):
        decl = named._read("pilot")
        writer = named._project_catalog(decl)
        reader = named._project_catalog(decl, reader=True)
        self.assertIn('runtime_role = "conix_runtime"', writer)
        self.assertIn('runtime_role = "conix_reader"', reader)
        self.assertIn('abi_file = "abi/token.json"', reader)
        self.assertNotIn("reader.dsn", reader)
        self.assertNotIn("owner.dsn", reader)
        self.assertEqual(
            writer.replace('runtime_role = "conix_runtime"', ""),
            reader.replace('runtime_role = "conix_reader"', ""),
        )

    def test_prepare_creates_separate_lock_and_stages_only_reader_dsn(self):
        self.dispatch("deployment/reader/prepare")
        reader = named._lock(named._read("pilot"), reader=True)
        self.assertEqual(reader["image"], IMAGE)
        self.assertEqual(reader["volume"], VOLUME_NEW)
        self.assertEqual(named._lock(named._read("pilot"))["volume"], VOLUME)
        self.assertTrue((self.folder / "reader.lock.toml").exists())
        self.assertEqual((self.folder / "reader.lock.toml").stat().st_mode & 0o777, 0o600)
        call = next(x for x in ReaderDocker.seen[-1].calls if x[0] == "stage")
        self.assertEqual(call[-1], self.folder / "secrets" / "reader.dsn")
        self.assertIn('runtime_role = "conix_reader"', ReaderDocker.seen[-1].projected)
        self.assertNotIn("owner.dsn", ReaderDocker.seen[-1].projected)
        self.assertEqual((self.folder / "secrets" / "runtime.dsn").read_text(),
                         "postgresql://conix_runtime:writer-only@postgres/conix\n")

    def test_reader_preparation_does_not_require_stopping_the_indexer(self):
        with patch.object(named, "_inspect_existing", return_value=["active-indexer"]):
            self.dispatch("deployment/reader/prepare")
        self.assertTrue((self.folder / "reader.lock.toml").exists())

    def test_missing_reader_credential_is_refused_without_creating_resources(self):
        (self.folder / "secrets" / "reader.dsn").unlink()
        with self.assertRaises(OperationError):
            self.dispatch("deployment/reader/prepare")
        self.assertFalse((self.folder / "reader.lock.toml").exists())
        self.assertEqual(ReaderDocker.seen, [])

    def test_reader_credential_permissions_and_symlinks_are_not_accepted(self):
        path = self.folder / "secrets" / "reader.dsn"
        path.chmod(0o644)
        with self.assertRaises(OperationError):
            self.dispatch("deployment/reader/prepare")
        path.unlink()
        path.symlink_to(self.folder / "secrets" / "runtime.dsn")
        with self.assertRaises(OperationError):
            self.dispatch("deployment/reader/prepare")
        self.assertFalse((self.folder / "reader.lock.toml").exists())

    def test_reader_prepare_failure_keeps_previous_lock(self):
        named._write_lock(
            self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME},
            reader=True,
        )
        old = (self.folder / "reader.lock.toml").read_bytes()
        with patch.object(ReaderDocker, "stage_inputs", side_effect=OperationError("failed")):
            with self.assertRaises(OperationError):
                self.dispatch("deployment/reader/prepare")
        self.assertEqual((self.folder / "reader.lock.toml").read_bytes(), old)

    def test_reader_lock_is_independent_and_malformed_reader_lock_fails(self):
        named._write_lock(
            self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME},
            reader=True,
        )
        (self.folder / "reader.lock.toml").write_text('{"image":"wrong"}')
        with self.assertRaises(OperationError):
            self.dispatch("deployment/reader/check")
        self.assertEqual(ReaderDocker.seen, [])
        self.assertEqual(named._lock(named._read("pilot"))["volume"], VOLUME)

    def test_reader_check_uses_only_reader_lock_and_two_selected_bridges(self):
        named._write_lock(
            self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME_NEW},
            reader=True,
        )
        self.dispatch("deployment/reader/check")
        docker = ReaderDocker.seen[-1]
        self.assertIn(
            ("reader_check", "pilot", IMAGE, VOLUME_NEW, "db-conix", "rpc-conix"),
            docker.calls,
        )
        self.assertNotIn(("reader_check", "pilot", IMAGE, VOLUME, "db-conix", "rpc-conix"),
                         docker.calls)

    def test_removal_retires_reader_only_after_confirmed_exact_volume(self):
        named._write_lock(
            self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME_NEW},
            reader=True,
        )
        self.dispatch("deployment/reader/remove")
        self.assertFalse((self.folder / "reader.lock.toml").exists())
        self.assertTrue((self.folder / "deployment.lock.toml").exists())
        self.assertIn(("remove_volume", VOLUME_NEW), ReaderDocker.seen[-1].calls)
        self.assertNotIn(("remove_volume", VOLUME), ReaderDocker.seen[-1].calls)

    def test_failed_reader_volume_retirement_keeps_recovery_lock(self):
        named._write_lock(
            self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME_NEW},
            reader=True,
        )
        with patch.object(ReaderDocker, "remove_inputs", side_effect=OperationError("refused")):
            with self.assertRaises(OperationError):
                self.dispatch("deployment/reader/remove")
        self.assertTrue((self.folder / "reader.lock.toml").exists())
        self.assertEqual(named._lock(named._read("pilot"))["volume"], VOLUME)

    def test_reader_lock_cannot_retire_the_writer_owned_volume(self):
        # Even an operator-owned but mistaken Reader lock grants no authority
        # to delete the separately selected indexer input volume.
        named._write_lock(
            self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME},
            reader=True,
        )
        with self.assertRaises(OperationError):
            self.dispatch("deployment/reader/remove")
        self.assertTrue((self.folder / "reader.lock.toml").exists())
        self.assertTrue((self.folder / "deployment.lock.toml").exists())
        self.assertEqual(ReaderDocker.seen, [])

    def test_reader_status_never_prints_credentials(self):
        named._write_lock(
            self.folder, {"version": 1, "image": IMAGE, "volume": VOLUME_NEW},
            reader=True,
        )
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            self.dispatch("deployment/reader/status")
        result = output.getvalue()
        self.assertIn(VOLUME_NEW, result)
        self.assertIn(IMAGE, result)
        self.assertNotIn("writer-only", result)
        self.assertNotIn("private@postgres", result)
        self.assertNotIn("owner", result)
        self.assertEqual(ReaderDocker.seen, [])


if __name__ == "__main__":
    unittest.main()
