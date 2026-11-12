"""The host input-staging owner has no deployment or Docker-selection API."""

import os
from pathlib import Path
import tempfile
import unittest

from tools.containers.input_staging import InputStaging
from tools.containers.client import OperationError


class InputStagingOwnershipContracts(unittest.TestCase):
    def test_snapshot_selects_only_declared_abis_without_docker_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "abi").mkdir()
            selected = root / "abi" / "selected.json"
            selected.write_bytes(b"[]\n")
            excluded = root / "abi" / "private.json"
            excluded.write_bytes(b"DO NOT INCLUDE\n")
            definition = root / "config.toml"
            definition.write_bytes(
                b'[contracts.token]\nabi_file = "abi/selected.json"\n'
            )
            # The admission contract requires non-writable sources under any host umask.
            selected.chmod(0o644)
            definition.chmod(0o644)
            stager = InputStaging()
            configuration, members = stager.catalog_snapshot(definition)
            self.assertIn(b"abi/selected.json", configuration)
            self.assertEqual(members, {"selected.json": b"[]\n"})
            self.assertNotIn("private.json", members)
            self.assertFalse(hasattr(stager, "run"),
                             "Host staging snapshots must not require Engine access.")

    def test_owner_credentials_reject_group_read_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "owner.dsn"
            secret.write_text("postgresql://owner:private@postgres/db\n")
            stager = InputStaging()
            secret.chmod(0o644)
            with self.assertRaises(OperationError):
                stager.private_bytes(secret, 8192, secret=True)
            secret.chmod(0o600)
            self.assertEqual(
                stager.private_bytes(secret, 8192, secret=True),
                b"postgresql://owner:private@postgres/db\n",
            )
            selected = root / "other.dsn"
            selected.symlink_to(secret)
            with self.assertRaises(OperationError):
                stager.private_bytes(selected, 8192, secret=True)


if __name__ == "__main__":
    unittest.main()
