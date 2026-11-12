"""Tool boundaries are packages, not a flat executable-script bucket.

These are behavioral import/CLI contracts. Every Docker entry point is
introspectable without selecting an Engine or acquiring a wheel.
"""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent

TOOL_FILES = {
    "tools/__init__.py",
    "tools/containers/__init__.py",
    "tools/containers/client.py",
    "tools/containers/diagnostics.py",
    "tools/containers/deployment.py",
    "tools/containers/history.py",
    "tools/containers/named.py",
    "tools/containers/operator.py",
    "tools/fixtures/__init__.py",
    "tools/fixtures/pay_to_play.py",
    "tools/fixtures/protocol.py",
    "tools/fixtures/process.py",
    "tools/fixtures/deployment.py",
    "tools/wheelhouse/__init__.py",
    "tools/wheelhouse/acquire_tool.py",
    "tools/wheelhouse/locks.py",
    "tools/wheelhouse/operations.py",
    "tools/wheelhouse/policy.py",
    "tools/wheelhouse/selector.py",
}
ENTRYPOINTS = {
    "tools.containers.operator",
    "tools.fixtures.pay_to_play",
    "tools.wheelhouse.locks",
    "tools.wheelhouse.selector",
}


class ToolLayoutContracts(unittest.TestCase):
    """Keep operator packages importable without starting a Docker process."""

    def test_all_tools_have_a_clear_owner(self):
        existing = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "tools").rglob("*.py")
            if "__pycache__" not in path.parts
        }
        self.assertEqual(existing, TOOL_FILES)

    def test_module_entrypoints_are_importable_without_docker(self):
        for module in sorted(ENTRYPOINTS):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())

    def test_importing_operators_has_no_container_effects(self):
        """Import is read-only: engine selection must remain an explicit action."""
        script = (
            "from tools.containers.operator import Docker, OperationError;"
            "from tools.fixtures.pay_to_play import Campaign;"
            "from tools.wheelhouse.operations import SharedWheelhouse;"
            "from tools.wheelhouse.policy import PoolError;"
            "assert Docker and OperationError and Campaign and SharedWheelhouse and PoolError"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys;sys.path.insert(0, " + repr(str(ROOT)) + ");" + script],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
