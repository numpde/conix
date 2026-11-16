"""The public host parser admits only fixed, bounded process-failure records."""

import unittest

from tools.containers.diagnostics import admitted, explanation


class ProcessDiagnosticContracts(unittest.TestCase):
    def test_nested_child_exit_is_bounded_and_survives_selector_to_host(self):
        import io
        import subprocess
        import sys
        from contextlib import redirect_stderr
        from pathlib import Path
        from unittest.mock import patch
        from tools.containers import client
        from tools.wheelhouse import selector

        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["selector", "--pool", "/pool", "--release", "/release",
                                      "--runtime", "/runtime", "--build", "/build"]),
            patch.object(selector, "run", side_effect=subprocess.CalledProcessError(23, "PRIVATE_COMMAND")),
            redirect_stderr(output),
        ):
            self.assertEqual(selector.main(), 2)
        fields = admitted(output.getvalue().encode())
        self.assertEqual(fields["child_exit"], 23)
        completed = subprocess.CompletedProcess(["docker"], 2, stdout="", stderr=output.getvalue())
        with patch.object(client.subprocess, "run", return_value=completed):
            with self.assertRaises(client.OperationError) as caught:
                client.command(["docker", "run"], env={}, working_dir=Path("."), capture=True)
        self.assertEqual(admitted(caught.exception.diagnostic)["child_exit"], 23)
        self.assertIn("child_exit=23", str(caught.exception))
        self.assertNotIn("PRIVATE_COMMAND", str(caught.exception) + output.getvalue())

    def test_known_record_preserves_cause_phase_and_uncertain_commit(self):
        payload = b'CONIX-FAILURE-V1 {"phase":"advance","cause":"CommitOutcomeUnknown","effect":"commit_unknown"}\n'
        self.assertEqual(admitted(payload), {
            "phase": "advance", "cause": "CommitOutcomeUnknown", "effect": "commit_unknown",
        })
        result = explanation(payload, 9, phases={"advance"})
        self.assertIn("cause=CommitOutcomeUnknown", result)
        self.assertIn("PostgreSQL COMMIT outcome unknown", result)
        self.assertIn("phase=advance", result)
        self.assertIn("exit=9", result)

    def test_optional_child_exit_refuses_non_numeric_and_unbounded_facts(self):
        import json
        from tools.containers.diagnostics import PREFIX
        fields = {"phase": "wheel_selection", "cause": "CalledProcessError",
                  "effect": "not_started", "location": "unavailable"}
        for value in (True, "PRIVATE", None, -256, 256):
            with self.subTest(kind=type(value).__name__):
                payload = PREFIX + json.dumps(dict(fields, child_exit=value)).encode()
                self.assertIsNone(admitted(payload))
        for value in (-9, 23):
            self.assertEqual(admitted(PREFIX + json.dumps(dict(fields, child_exit=value)).encode())["child_exit"], value)

    def test_unreviewed_data_is_not_admitted(self):
        valid = b'CONIX-FAILURE-V1 {"phase":"verification","cause":"InputError","effect":"not_started"}\n'
        self.assertIsNotNone(admitted(valid))
        for input_bytes in (
            valid + b"password=unsafe",
            b"prefix " + valid,
            valid.replace(b"InputError", b"made_up"),
            valid.replace(b"verification", b"owner_secret"),
            valid.replace(b"not_started", b"password"),
            valid.replace(b"}", b',"message":"sensitive"}'),
            valid + b"\n",
            valid + b"\x1b[2J",
            b"CONIX-FAILURE-V1 " + b"A" * 1000,
            b'CONIX-FAILURE-V1 {"phase":"advance","phase":"verification","cause":"InputError","effect":"not_started"}',
        ):
            with self.subTest(payload=input_bytes[:40]):
                self.assertIsNone(admitted(input_bytes))

    def test_unexpected_type_requires_safe_source_location(self):
        case = b'CONIX-FAILURE-V1 {"phase":"staged_inputs","cause":"ValueError","effect":"not_started","location":"input_staging.py:233"}'
        self.assertEqual(admitted(case)["cause"], "ValueError")
        self.assertIn("location=input_staging.py:233", explanation(
            case, 2, phases={"staged_inputs"},
        ))
        for payload in (
            case.replace(b',"location":"input_staging.py:233"', b""),
            case.replace(b"input_staging.py:233", b"/etc/shadow:5"),
            case.replace(b"input_staging.py:233", b"secret\\nname.py:5"),
            case.replace(b"input_staging.py:233", b"input_staging.py:0"),
            case.replace(b"ValueError", b"Private/Password"),
            case.replace(b"input_staging.py:233", b"input_staging.py:1234567890"),
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(admitted(payload))

    def test_staging_diagnostic_cannot_impersonate_history_worker(self):
        payload = b'CONIX-FAILURE-V1 {"phase":"staging","cause":"InputError","effect":"unconfirmed"}'
        self.assertIsNone(explanation(payload, 2, phases={"advance", "staged_inputs"}))
        self.assertIn("phase=staging", explanation(payload, 2, phases={"staging"}))


    def test_operator_record_preserves_the_admitted_producer_failure(self):
        from tools.containers.client import OperationError
        from tools.containers.diagnostics import error_record
        record = b'CONIX-FAILURE-V1 {"phase":"reader_check","cause":"StorageUnavailable","effect":"unconfirmed"}'
        error = OperationError("Reader check failed.", diagnostic=record, exit_code=2)
        self.assertEqual(admitted(error_record(error)), dict(admitted(record), child_exit=2))

    def test_nested_process_wrappers_keep_the_innermost_observed_exit(self):
        from tools.containers.client import OperationError
        from tools.containers.diagnostics import error_record
        from tools.fixtures.process import CampaignError

        record = b'CONIX-FAILURE-V1 {"phase":"run","cause":"StorageUnavailable","effect":"unconfirmed"}'
        child = error_record(OperationError("owned child failed", diagnostic=record, exit_code=17))
        wrapped = error_record(CampaignError("Make failed", diagnostic=child, exit_code=2))
        self.assertEqual(admitted(wrapped)["child_exit"], 17)
        self.assertIn("exit=2", explanation(wrapped, 2, phases={"run"}))

    def test_recovered_child_exit_does_not_become_its_wrapper_exit(self):
        from tools.containers.client import OperationError
        from tools.containers.diagnostics import error_record
        from tools.containers.process_evidence import preserve_primary
        from contextlib import redirect_stderr
        import io

        primary = OperationError("Reader attach failed.", exit_code=2)
        facts = {"exit_code": 17, "diagnostic": {"phase": "reader_check",
                 "cause": "StorageUnavailable", "effect": "unconfirmed"}}
        with redirect_stderr(io.StringIO()):
            retained = preserve_primary(primary, facts, "Reader")
        self.assertIs(retained, primary)
        self.assertEqual(retained.exit_code, 2)
        self.assertEqual(admitted(error_record(retained))["child_exit"], 17)

    def test_host_fallback_does_not_stringify_exception_values(self):
        from tools.containers.diagnostics import error_record
        class HostileFailure(RuntimeError):
            def __str__(self):
                raise AssertionError("Do not stringify private values.")
        try:
            raise HostileFailure()
        except HostileFailure as error:
            record = admitted(error_record(error))
        self.assertEqual(record["cause"], "HostileFailure")
        self.assertEqual(record["phase"], "operator")
        self.assertIn("test_process_diagnostics.py:", record["location"])


    def test_admitted_json_encoding_cannot_break_terminal_failure_emission(self):
        from tools.containers.client import OperationError
        from tools.containers.diagnostics import PREFIX, error_record
        source = PREFIX + b'\xef\xbb\xbf' + b'{"phase":"reader_check","cause":"InputError","effect":"not_started"}'
        self.assertIsNotNone(admitted(source))
        emitted = error_record(OperationError("Reader failed.", diagnostic=source))
        emitted.decode("ascii")
        self.assertEqual(admitted(emitted), admitted(source))


if __name__ == "__main__":
    unittest.main()
