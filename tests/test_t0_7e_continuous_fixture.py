"""Acceptance-only source projection for the real continuous dapp32 phase."""

from pathlib import Path
import stat
import tempfile
import unittest

from conix.catalog import prepare_catalog
from generic_acceptance import catalog_path, stage_continuous_inputs


class ContinuousDapp32SourceContracts(unittest.TestCase):
    def test_stage_three_real_abi_selections_without_schema_owner(self):
        descriptor = {
            "chainId": 31337,
            "rpcUrl": "http://anvil:8545",
            "contracts": {"PayToPlay": {
                "address": "0x" + "11" * 20, "deploymentBlock": 1,
            }},
        }
        tokens = {
            "token20": {"address": "0x" + "22" * 20},
            "token721": {"address": "0x" + "33" * 20},
        }
        original = catalog_path(descriptor, "0x" + "44" * 32, tokens)
        runtime_dsn = "postgresql://runtime:disposable@postgres/conix_test"

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            source = stage_continuous_inputs(original, runtime_dsn, target)
            projected = prepare_catalog(target / "config.toml")

            self.assertEqual(projected.source_id, source.source_id)
            self.assertEqual(
                {selected.alias for selected in projected.contracts},
                {"payment", "token20", "token721"},
            )
            self.assertEqual(
                {path.name for path in (target / "abi").iterdir()},
                {"payment.json", "token20.json", "token721.json"},
            )
            self.assertEqual(
                (target / "connection.dsn").read_text(), runtime_dsn + "\n",
            )
            self.assertEqual(
                stat.S_IMODE((target / "connection.dsn").stat().st_mode), 0o600,
            )
            self.assertFalse((target / "owner.dsn").exists())
            self.assertNotIn("disposable", (target / "config.toml").read_text())
            self.assertEqual(set(path.name for path in target.iterdir()), {
                "abi", "config.toml", "connection.dsn",
            })


if __name__ == "__main__":
    unittest.main()
