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
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import shared_wheelhouse as pool


class SharedWheelhouseContracts(unittest.TestCase):
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
        self.assertIn("tests/fixture_campaign.py", selected)
        self.assertNotIn("tools/container.py", selected)
        self.assertNotIn("containers/wheelhouse/Dockerfile", selected)
        self.assertTrue(all(item["sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
                            for item in sources))

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

    def test_acquisition_and_offline_workers_have_nonoverlapping_permissions(self):
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
