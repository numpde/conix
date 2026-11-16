"""Binary attachments retain private evidence without daemon log disclosure."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from tools.containers.process_evidence import retain_attached_exit
from tools.containers.diagnostics import PREFIX
from tools.containers.client import OperationError
from tools.fixtures.process import CampaignError, clean_environment, owned_command


class BinaryProcessEvidenceContracts(unittest.TestCase):
    def test_real_binary_child_is_retained_without_loading_a_control_receipt(self):
        """A WARC-sized stdout survives while the producer diagnostic stays separate."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, errors = root / "capture.warc.gz", root / "capture.err"
            data = b"WARC\x00" * 300000
            fields = {"phase": "operator", "cause": "ValueError", "effect": "not_started", "location": "fixture.py:1"}
            diagnostic = PREFIX.decode("ascii") + json.dumps(fields) + "\n"
            child = [sys.executable, "-c",
                     "import sys; sys.stdout.buffer.write(b'WARC\\x00' * 300000); "
                     f"sys.stderr.write({diagnostic!r})"]
            result = owned_command(child, output=output, error_file=errors, env=clean_environment(),
                                   timeout=10, binary_output_limit=len(data))
            self.assertIsNone(result)
            self.assertEqual(output.read_bytes(), data)
            docker = SimpleNamespace(working_dir=root)
            record = {
                "Id": "a" * 64, "Image": "sha256:" + "b" * 64, "RestartCount": 0,
                "State": {"Running": False, "ExitCode": 0, "OOMKilled": False,
                          "StartedAt": "2026-11-16T00:00:00Z", "FinishedAt": "2026-11-16T00:00:01Z"},
            }
            facts = retain_attached_exit(docker, record, output=output, error_file=errors,
                                         output_limit=len(data), attachment_complete=True)
            self.assertEqual(facts["stdout_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(facts["stdout_bytes"], len(data))
            self.assertTrue(facts["attachment_complete"])
            self.assertEqual(facts["diagnostic"], fields)
            self.assertTrue((root / "out/process-evidence" / ("a" * 64 + ".json")).exists())

    def test_binary_grant_does_not_relax_the_default_control_receipt_limit(self):
        """Large output needs an explicit grant and remains private on refusal."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(CampaignError):
                owned_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*1048577)"],
                              output=root / "output", error_file=root / "error", env=clean_environment(), timeout=10)
            self.assertEqual((root / "output").stat().st_size, 1048577)
            self.assertEqual((root / "output").stat().st_mode & 0o777, 0o600)

    def test_binary_content_cannot_supply_the_producer_failure_diagnostic(self):
        """Archived provider text has no authority over terminal failure reports."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forged = PREFIX + b'{"phase":"advance","cause":"WriterBusy","effect":"not_started"}\n'
            with self.assertRaises(CampaignError) as caught:
                owned_command([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({forged!r}); sys.exit(7)"],
                              output=root / "output", error_file=root / "error", env=clean_environment(),
                              timeout=10, binary_output_limit=1048576)
            self.assertEqual(caught.exception.exit_code, 7)
            self.assertIsNone(caught.exception.diagnostic)
            self.assertNotIn("WriterBusy", str(caught.exception))

    def test_binary_output_over_its_explicit_grant_is_not_success(self):
        """Keep oversized evidence private without authorizing its admission."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(CampaignError):
                owned_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'12345')"],
                              output=root / "output", error_file=root / "error", env=clean_environment(),
                              timeout=10, binary_output_limit=4)
            self.assertEqual((root / "output").read_bytes(), b"12345")
            self.assertEqual((root / "output").stat().st_mode & 0o777, 0o600)

    def test_unavailable_attachment_preserves_the_known_exit_and_diagnostic(self):
        """Retention refusal must not turn a known producer error into a generic IO failure."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = root / "error"
            errors.write_bytes(PREFIX + b'{"phase":"advance","cause":"WriterBusy","effect":"not_started"}\n')
            errors.chmod(0o600)
            record = {"Id": "c" * 64, "Image": "sha256:" + "b" * 64, "RestartCount": 0,
                      "State": {"Running": False, "ExitCode": 9, "OOMKilled": False,
                                "StartedAt": "2026-11-16T00:00:00Z", "FinishedAt": "2026-11-16T00:00:01Z"}}
            with self.assertRaises(OperationError) as caught:
                retain_attached_exit(SimpleNamespace(working_dir=root), record, output=root / "missing",
                                     error_file=errors, output_limit=1048576, attachment_complete=False)
            self.assertEqual(caught.exception.exit_code, 9)
            self.assertIsNotNone(caught.exception.diagnostic)
            self.assertIn("retention is unconfirmed", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
