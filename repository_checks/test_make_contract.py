"""Exact, stable resource/action Make API: no aliases or implementation paths.

This is deliberately an exported-API test, independent of the dispatcher.
"""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {
    "help",
    "test/repository/run", "repository/inputs/check",
    "image/python-base/acquire", "image/postgres/acquire",
    "image/dockerfile-frontend/acquire", "image/foundry/acquire",
    "wheelhouse/check", "wheelhouse/acquire",
    "image/indexer/build", "image/test-runner/build", "image/evm-test-runner/build",
    "image/indexer/check", "image/evm-test-runner/check",
    "test/postgres/run", "test/input-staging/run",
    "test/indexer/acceptance/run", "campaign/indexer/acceptance/run",
    "deployment/inputs/create", "deployment/inputs/remove",
"deployment/config/check",
"deployment/status", "deployment/remove",
}


class MakeContractTests(unittest.TestCase):
    """Treat published Make target names as a stable operator interface."""

    def test_exact_exported_target_names(self):
        text = (ROOT / "Makefile").read_text()
        actual = set()
        for line in text.splitlines():
            found = re.fullmatch(r"([a-z][a-z0-9/-]*(?: [a-z][a-z0-9/-]*)*):", line)
            if found:
                actual.update(found[1].split())
        self.assertEqual(actual, TARGETS)

    def test_literal_input_capture_and_no_legacy_selectors(self):
        source = (ROOT / "Makefile").read_text()
        for name in (
            "DOCKER_CONTEXT", "WHEELHOUSE", "IMAGE", "RUNNER_IMAGE",
            "INDEXER_IMAGE", "FIXTURE_DESCRIPTOR", "DAPP32_DIR",
            "DEPLOYMENT", "INPUT_VOLUME", "CONFIG_FILE", "DSN_FILE",
            "DATABASE_NETWORK", "RPC_NETWORK",
        ):
            with self.subTest(name=name):
                self.assertIn("CONIX_MAKE_" + name + " := $(value " + name + ")", source)
        for deprecated in ("CONIX_IMAGE", "CONIX_TEST_IMAGE", "CONIX_FIXTURE_IMAGE",
                           "CONIX_CONFIG_FILE", "CONIX_DSN_FILE", "CONIX_DEPLOYMENT",
                           "WHEELHOUSE_IMAGE"):
            self.assertNotIn("CONIX_MAKE_" + deprecated, source)


if __name__ == "__main__":
    unittest.main()
