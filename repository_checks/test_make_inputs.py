"""Make treats operator values as data, including hostile shell and Make syntax.

The fake interpreter records the exact process environment; it never contacts
Docker. A malicious argument may be refused later by the Python operator, but
must not execute during Make's expansion or its shell recipes.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent

PARAMETERS = {
    "image/python-base/acquire": "DOCKER_CONTEXT",
    "image/postgres/acquire": "DOCKER_CONTEXT",
    "image/dockerfile-frontend/acquire": "DOCKER_CONTEXT",
    "image/foundry/acquire": "DOCKER_CONTEXT",
    "wheelhouse/check": "WHEELHOUSE",
    "wheelhouse/acquire": "WHEELHOUSE",
    "image/indexer/build": "WHEELHOUSE",
    "image/test-runner/build": "WHEELHOUSE",
    "image/evm-test-runner/build": "WHEELHOUSE",
    "image/indexer/check": "IMAGE",
    "image/evm-test-runner/check": "IMAGE",
    "test/indexer/run": "RUNNER_IMAGE",
    "test/deployment/acceptance/run": "DEPLOYMENT",
    "test/input-staging/run": "DSN_FILE",
    "test/indexer/acceptance/borrowed/run": "FIXTURE_DESCRIPTOR",
    "test/indexer/acceptance/owned/run": "DAPP32_DIR",
    "history/advance": "INPUT_VOLUME",
    "deployment/inputs/create": "CONFIG_FILE",
    "deployment/inputs/remove": "INPUT_VOLUME",
    "deployment/init": "DOCKER_CONTEXT",
    "deployment/prepare": "DEPLOYMENT",
    "deployment/database/init": "DEPLOYMENT",
    "deployment/config/check": "DEPLOYMENT",
    "deployment/up": "DEPLOYMENT",
    "deployment/status": "DEPLOYMENT",
    "deployment/logs": "DEPLOYMENT",
    "deployment/down": "DEPLOYMENT",
}


class MakeInputSafety(unittest.TestCase):
    """Operator text must cross Make literally, never execute during expansion."""

    def test_literal_input_campaign_covers_every_public_operator_target(self):
        """A newly exported operator target must not silently miss injection tests."""
        from test_make_contract import TARGETS

        direct_checks = {"help", "test/repository/run", "repository/inputs/check"}
        self.assertEqual(set(PARAMETERS), TARGETS - direct_checks)

    def test_default_target_is_read_only_help(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "effect"
            env = os.environ.copy()
            env["DOCKER_CONTEXT"] = "$(shell touch " + str(marker) + ")"
            finished = subprocess.run(
                ["make", "--no-print-directory"], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(finished.returncode, 0, finished.stderr)
            self.assertIn("conix make api", finished.stdout.lower())
            self.assertFalse(marker.exists(), "Plain make executed an operator-controlled expression.")

    def test_operator_values_cross_make_without_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "python3"
            trace = root / "trace.json"
            fake.write_text(
                "#!" + sys.executable + "\n"
                "import json,os,sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['CONIX_TEST_TRACE']).write_text(json.dumps({\n"
                " 'argv':sys.argv[1:],\n"
                " 'vars':{k:v for k,v in os.environ.items() if k.startswith('CONIX_MAKE_')}\n"
                "}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(root) + os.pathsep + env["PATH"]
            env["CONIX_TEST_TRACE"] = str(trace)
            # Clear inherited selections so the test proves Make does not
            # quietly recover authority from the operator's environment.
            for key in PARAMETERS.values():
                env.pop(key, None)
            for name in list(env):
                if name.startswith("CONIX_MAKE_"):
                    env.pop(name)
            for (target, key) in PARAMETERS.items():
                for shape in ("make", "shell"):
                    with self.subTest(target=target, variable=key, payload=shape):
                        marker = root / (target.replace("/", "_") + "_" + shape)
                        value = (
                            "$(shell touch " + str(marker) + ")"
                            if shape == "make"
                            else '"; touch ' + str(marker) + '; echo "'
                        )
                        arguments = ["make", "--no-print-directory", "--silent", target,
                                     "DOCKER_CONTEXT=science", key + "=" + value]
                        finished = subprocess.run(
                            arguments, cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=10,
                        )
                        self.assertEqual(finished.returncode, 0, finished.stderr)
                        self.assertFalse(marker.exists(), "Make/shell executed data as code.")
                        self.assertTrue(trace.is_file(), "Make did not reach the operator entry point.")
                        record = json.loads(trace.read_text(encoding="utf-8"))
                        self.assertEqual(record["vars"]["CONIX_MAKE_" + key], value)
                        expected = (
                            "tools.fixtures.pay_to_play" if target in ("test/indexer/acceptance/borrowed/run", "test/indexer/acceptance/owned/run")
                            else "tools.containers.operator"
                        )
                        self.assertEqual(record["argv"][:2], ["-m", expected])
                        if target in ("test/indexer/acceptance/borrowed/run", "test/indexer/acceptance/owned/run"):
                            self.assertIn("--from-make", record["argv"])
                        else:
                            self.assertIn(target, record["argv"])


if __name__ == "__main__":
    unittest.main()
