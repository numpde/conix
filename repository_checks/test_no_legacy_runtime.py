"""Regression gate: no obsolete PayToPlay runtime or CLI may reappear."""

from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parent.parent
REMOVED = {
    "src/conix/chain.py", "src/conix/commands.py", "src/conix/configuration.py",
    "src/conix/engine.py", "src/conix/errors.py", "src/conix/journal.py",
    "src/conix/models.py", "src/conix/observer.py", "src/conix/pay_to_play.py",
    "src/conix/rpc.py", "src/conix/schema.py",
    "tests/pay_to_play_integration.py", "tests/prepare_schema.py",
    "tests/test_tier0.py", "tests/test_abi_type_safety.py",
    "tests/test_journal_type_safety.py", "tests/test_position_type_safety.py",
    "tests/test_rpc_type_safety.py",
}


class PrototypeRemovalContracts(unittest.TestCase):

    def test_removed_module_paths_do_not_survive(self):
        for path in sorted(REMOVED):
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists(), path)

    def test_installed_package_has_no_old_cli_or_prototype_imports(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertNotIn("scripts", project["project"])
        source = ROOT / "src" / "conix"
        self.assertEqual(
            {path.name for path in source.glob("*.py")},
            {"__init__.py", "abi.py", "input_staging.py", "installation_check.py"},
        )
        self.assertIn("src/conix/abi.py", {
            path.relative_to(ROOT).as_posix() for path in source.glob("*.py")
        })
        image = (ROOT / "Dockerfile").read_text()
        self.assertNotIn('ENTRYPOINT ["conix"]', image)
        self.assertNotIn("pay_to_play_integration.py", image)

    def test_public_make_targets_cannot_start_deleted_engine(self):
        make = (ROOT / "Makefile").read_text()
        for obsolete in (
            "database/schema/initialize", "deployment/apply",
            "test/pay-to-play/run", "campaign/pay-to-play/run",
        ):
            with self.subTest(target=obsolete):
                self.assertNotIn(obsolete, make)
        self.assertNotIn("conix.commands", make)
        self.assertIn("test/indexer/acceptance/run", make)
