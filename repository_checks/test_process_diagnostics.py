"""The public host parser admits only fixed, bounded process-failure records."""

import unittest

from tools.containers.diagnostics import admitted, explanation


class ProcessDiagnosticContracts(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
