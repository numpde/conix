"""Regression gate: no obsolete PayToPlay runtime or CLI may reappear."""

from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parent.parent


class PrototypeRemovalContracts(unittest.TestCase):

    def test_no_obsolete_entrypoint_is_published(self):
        """Check public dispatch, not an allowlist of future module filenames."""
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertNotIn("scripts", project["project"])

        image = (ROOT / "Dockerfile").read_text()
        self.assertNotIn('ENTRYPOINT ["conix"]', image)

    def test_public_make_targets_cannot_start_deleted_engine(self):
        make = (ROOT / "Makefile").read_text()
        for obsolete in (
            "database/schema/initialize", "deployment/apply",
            "test/pay-to-play/run", "campaign/pay-to-play/run",
        ):
            with self.subTest(target=obsolete):
                self.assertNotIn(obsolete, make)
        self.assertNotIn("conix.commands", make)
        self.assertIn("test/indexer/acceptance/borrowed/run", make)
