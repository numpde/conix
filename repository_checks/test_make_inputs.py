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
    "deps/images/prepare": "DOCKER_CONTEXT",
    "image/build": "WHEELHOUSE_IMAGE",
    "image/smoke": "CONIX_IMAGE",
    "tests/container": "CONIX_TEST_IMAGE",
    "inputs/stage": "CONIX_CONFIG_FILE",
    "inputs/proof": "CONIX_DSN_FILE",
    "inputs/remove": "CONIX_INPUT_VOLUME",
    "schema/init": "CONIX_OWNER_DSN_FILE",
    "runtime/start": "CONIX_DEPLOYMENT",
    "runtime/stop": "CONIX_DATABASE_NETWORK",
    "runtime/status": "CONIX_RPC_NETWORK",
}


class MakeInputSafety(unittest.TestCase):
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
            self.assertIn("conix container commands", finished.stdout.lower())
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
            # Clear incidental inherited Make variables to test no hidden
            # substitution or unexpected live input authority.
            for key in PARAMETERS.values():
                env.pop(key, None)
            for name in list(env):
                if name.startswith("CONIX_MAKE_"):
                    env.pop(name)
            for target, key in PARAMETERS.items():
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
                        self.assertIn("tools/container.py", record["argv"][0])
                        self.assertIn(target, record["argv"])


if __name__ == "__main__":
    unittest.main()
