"""Behavioral contracts for the NMR shared wheelhouse admission boundary.

No Docker daemon, wheel acquisition or host pip installation. The same tests
must pass on a warm shared pool and a never-used checkout.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
from tools.wheelhouse import policy as pool


class SharedWheelhouseContracts(unittest.TestCase):
    """Prove admission and least privilege without a real package index."""

    def test_explicit_existing_private_pool_and_no_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "wheels"
            cache.mkdir(mode=0o700)
            self.assertEqual(pool.admit_pool(str(cache)), cache)
            for value in ("", ".", "/", "wheels", str(root / "absent")):
                with self.subTest(path=value), self.assertRaises(pool.PoolError):
                    pool.admit_pool(value)
            link = root / "alias"
            link.symlink_to(cache, target_is_directory=True)
            with self.assertRaises(pool.PoolError):
                pool.admit_pool(str(link))

    def test_git_inventory_refuses_untracked_python_inputs(self):
        with patch.object(
            pool.subprocess, "run",
            return_value=SimpleNamespace(stdout=b"src/conix/__init__.py\0"),
        ):
            with self.assertRaises(pool.PoolError):
                pool.source_allowlist(ROOT)

    def test_selected_engine_determines_pool_worker_identity(self):
        from tools.wheelhouse import operations as operation

        class Engine:
            """Model only rootless identity and a contained selector response."""

            def __init__(self, rootless):
                self.calls = []
                self.rootless = rootless
                self.base = ["docker", "--config", "/private/client",
                             "--host", "unix:///tmp/selected-docker.sock"]
                self.env = {}

            def run(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                if args[:2] == ("info", "--format"):
                    return '["name=rootless"]' if self.rootless else '["name=seccomp"]'
                if args and args[0] == "run":
                    return json.dumps([{
                        "directory": "wheelhouse",
                        "filename": "demo-1.0-py3-none-any.whl",
                        "sha256": "a" * 64,
                    }])
                raise AssertionError("Unexpected Docker method in offline wheel worker.")

            def require_image(self, reference):
                return "sha256:" + "b" * 64

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "wheels"
            cache.mkdir(mode=0o700)
            for rootless in (False, True):
                with self.subTest(rootless=rootless):
                    engine = Engine(rootless)
                    with patch.object(operation, "has_tool", return_value=True):
                        runner = operation.SharedWheelhouse(
                            engine,
                            {"python_base": "docker.io/library/python@sha256:" + "b" * 64},
                            str(cache),
                        )
                        selected = runner.check()
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(runner.user, "0:0" if rootless else
                                     f"{os.geteuid()}:{os.getegid()}")
                    cmd, kwargs = engine.calls[-1]
                    self.assertEqual(cmd[:1], ("run",))
                    self.assertEqual(cmd[cmd.index("--network") + 1], "none")
                    self.assertEqual(cmd[cmd.index("--pull") + 1], "never")
                    self.assertEqual(cmd[cmd.index("--user") + 1], runner.user)
                    self.assertTrue(all("readonly" in value for value in cmd
                                        if value.startswith("type=bind,")))
                    self.assertFalse(kwargs.get("public_diagnostic"))
                    self.assertTrue(kwargs.get("capture"))

    def test_released_tool_is_same_pinned_nmr_artifact(self):
        pin = pool.release_record()
        self.assertEqual(pin["version"], "0.5.14")
        self.assertEqual(pin["sha256"], "9d8cd59c0aeffd61ce462fa81f2951a24fb8a8749a1332d5f87842b4c095d656")
        self.assertEqual(pin["name"], "wheelhouse-tool-0.5.14.pyz")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / pin["name"]
            self.assertFalse(pool.has_tool(root, pin))
            path.write_bytes(b"wrong tool bytes")
            with self.assertRaises(pool.PoolError):
                pool.has_tool(root, pin)

    def test_source_capture_is_exact_and_rejects_symlinks(self):
        sources = pool.source_allowlist(ROOT)
        selected = {item["destination"] for item in sources}
        self.assertIn("Dockerfile", selected)
        self.assertIn("requirements/runtime.lock", selected)
        self.assertIn("requirements/build.lock", selected)
        self.assertIn("src/conix/journal.py", selected)
        self.assertIn("tests/pay_to_play_integration.py", selected)
        self.assertNotIn("tools/containers/operator.py", selected)
        self.assertNotIn("containers/wheelhouse/Dockerfile", selected)
        self.assertTrue(all(item["sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
                            for item in sources))

    def test_normalized_source_lock_preserves_exact_one_line_hash_authority(self):
        source = (
            "# retained comment\n"
            "alpha==1.2.3 --hash=sha256:" + "a" * 64 + "\n"
            "beta==2.0.0 --hash=sha256:" + "b" * 64 + "\n"
        )
        normalized = pool.project_issuer_lock(source)
        self.assertEqual(normalized, (
            "alpha==1.2.3 \\\n    --hash=sha256:" + "a" * 64 + "\n"
            "beta==2.0.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"
        ))
        for bad in (
            "alpha>=1.2.3 --hash=sha256:" + "a" * 64,
            "alpha==1.2.3 --hash=sha256:bad",
            "alpha==1.2.3 --hash=sha256:" + "a" * 64 + " --extra",
            "alpha==1.2.3 --hash=sha256:" + "a" * 64 + "\n"
            "alpha==1.2.3 --hash=sha256:" + "a" * 64,
        ):
            with self.subTest(bad=bad), self.assertRaises(pool.PoolError):
                pool.project_issuer_lock(bad)

    def test_selected_manifest_requires_exact_unique_wheels(self):
        sha = "a" * 64
        name = "example-1.0-py3-none-any.whl"
        selected = [{"directory": "wheelhouse", "filename": name, "sha256": sha}]
        self.assertEqual(pool.selection_manifest(json.dumps(selected).encode()), selected)
        for changed in (
            [{"directory": "wheelhouse", "filename": name, "sha256": sha}, selected[0]],
            [{"directory": "../foreign", "filename": name, "sha256": sha}],
            [{"directory": "wheelhouse", "filename": "secret.txt", "sha256": sha}],
            [{"directory": "wheelhouse", "filename": name, "sha256": "wrong"}],
            [{"directory": "wheelhouse", "filename": "/tmp/outside.whl", "sha256": sha}],
        ):
            with self.subTest(changed=changed), self.assertRaises(pool.PoolError):
                pool.selection_manifest(json.dumps(changed).encode())

    def test_stream_failure_explanation_is_bounded_and_nonsecret(self):
        from tools.wheelhouse.operations import _public_stream_diagnostic
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "stream.err"
            trace.write_text("wheel selection rejected: missing pinned wheel\n")
            self.assertIn("missing pinned wheel", _public_stream_diagnostic(trace))
            trace.write_text("postgresql://user:password@host/secret\n")
            self.assertIsNone(_public_stream_diagnostic(trace))
            trace.write_text("privateKey=0x" + "ab" * 32 + "\n")
            self.assertIsNone(_public_stream_diagnostic(trace))

    def test_warm_failure_does_not_authorize_network_without_missing_code(self):
        from tools.wheelhouse import operations as operation
        fake = object.__new__(operation.SharedWheelhouse)
        fake.pin = {"name": "wheelhouse-tool-0.5.14.pyz"}
        fake.pool = Path("/not/used")
        for error in (ValueError("invalid lock"), RuntimeError("Docker failed")):
            with self.subTest(error=type(error).__name__), patch.object(
                operation, "has_tool", return_value=True
            ), patch.object(fake, "check", side_effect=error), patch.object(
                fake, "run", side_effect=AssertionError("Network permission improperly granted")
            ):
                with self.assertRaises(type(error)):
                    fake.acquire()

    def test_acquisition_and_offline_workers_have_nonoverlapping_permissions(self):
        # A warm check gets neither egress nor a writable pool; explicit
        # acquisition receives those permissions in a separate lane.
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "wheels"
            cache.mkdir()
            warm = pool.worker_grants(cache, user="1001:1001", network=False)
            cold = pool.worker_grants(cache, user="1001:1001", network=True)
            for args in (warm, cold):
                self.assertIn("--read-only", args)
                self.assertIn("--cap-drop", args)
                self.assertIn("ALL", args)
                self.assertIn("no-new-privileges:true", args)
                self.assertIn("1001:1001", args)
                self.assertNotIn("--privileged", args)
                self.assertNotIn("--network=host", args)
            self.assertEqual(warm[warm.index("--network") + 1], "none")
            self.assertEqual(cold[cold.index("--network") + 1], "bridge")
            self.assertTrue(any("readonly" in value for value in warm if value.startswith("type=bind")))
            self.assertFalse(any("readonly" in value for value in cold if value.startswith("type=bind")))


if __name__ == "__main__":
    unittest.main()
