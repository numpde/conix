"""Exercise installed diagnostic producers through the actual host decoder.

The test-runner contains an exact copy of the host decoder source, not a
second implementation. Both entrypoints are invoked with controlled failures,
and the resulting stderr is decoded as the operator would decode it.
"""

import io
import sys
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from conix import input_staging, worker
from host_diagnostics import admitted, explanation


PRIVATE = "postgresql://owner:super-private-password@postgres/conix"
TOKEN = "bearer-" + "b" * 56


def failure_from(action):
    """Capture one executable entrypoint's actual public failure record."""
    errors = io.StringIO()
    with redirect_stderr(errors), unittest.TestCase().assertRaises(SystemExit) as status:
        action()
    if status.exception.code != 2:
        raise AssertionError(f"Expected controlled failure exit 2, got {status.exception.code!r}")
    return errors.getvalue().encode("utf-8")


class InstalledFailurePipeline(unittest.TestCase):
    def test_worker_unexpected_input_error_preserves_phase_type_source_and_secrecy(self):
        def explode():
            raise ValueError(PRIVATE)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=explode),
        ):
            output = failure_from(worker.main)

        record = admitted(output)
        self.assertEqual(record["phase"], "staged_inputs")
        self.assertEqual(record["cause"], "ValueError")
        self.assertEqual(record["effect"], "not_started")
        self.assertRegex(record["location"], r"^test_process_failure_pipeline\.py:[1-9][0-9]*$")
        report = explanation(output, 2, phases={"staged_inputs", "advance"})
        self.assertIn("phase=staged inputs", report)
        self.assertIn("cause=ValueError", report)
        self.assertIn(record["location"], report)
        self.assertNotIn(PRIVATE, report)
        self.assertNotIn(PRIVATE.encode(), output)

    def test_worker_interrupt_during_input_loading_is_admitted_not_started(self):
        def interrupt():
            raise KeyboardInterrupt(TOKEN)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=interrupt),
        ):
            output = failure_from(worker.main)

        fields = admitted(output)
        self.assertEqual(fields, {
            "phase": "staged_inputs", "cause": "KeyboardInterrupt",
            "effect": "not_started",
        })
        report = explanation(output, 2, phases={"staged_inputs", "advance"})
        self.assertIn("cause=KeyboardInterrupt", report)
        self.assertIn("effect=operation did not start", report)
        self.assertNotIn(TOKEN, report)

    def test_worker_unexpected_advance_failure_preserves_type_and_safe_location(self):
        async def explode(*_args):
            raise RuntimeError(PRIVATE)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", return_value=(SimpleNamespace(), PRIVATE)),
            patch.object(worker, "advance", side_effect=explode),
        ):
            output = failure_from(worker.main)

        fields = admitted(output)
        self.assertEqual(fields["phase"], "advance")
        self.assertEqual(fields["cause"], "RuntimeError")
        self.assertEqual(fields["effect"], "unconfirmed")
        report = explanation(output, 2, phases={"staged_inputs", "advance"})
        self.assertIn("cause=RuntimeError", report)
        self.assertIn("effect=durable outcome unconfirmed", report)
        self.assertIn("location=test_process_failure_pipeline.py:", report)
        self.assertNotIn(PRIVATE, report)

    def test_verifier_unexpected_failure_preserves_actual_type_and_safe_location(self):
        def explode():
            raise LookupError(PRIVATE)

        with patch.object(input_staging, "load_runtime_inputs", side_effect=explode):
            output = failure_from(lambda: input_staging.main(["verify"]))

        fields = admitted(output)
        self.assertEqual(fields["phase"], "verification")
        self.assertEqual(fields["cause"], "LookupError")
        self.assertEqual(fields["effect"], "not_started")
        report = explanation(output, 2, phases={"verification"})
        self.assertIn("location=test_process_failure_pipeline.py:", report)
        self.assertIn("cause=LookupError", report)
        self.assertNotIn(PRIVATE, report)

    def test_stager_unexpected_failure_preserves_actual_type_and_uncertain_effect(self):
        def explode(_payload):
            raise RuntimeError(PRIVATE)

        stdin = SimpleNamespace(buffer=io.BytesIO(b"fake"))
        with (
            patch.object(input_staging.sys, "stdin", stdin),
            patch.object(input_staging, "stage", side_effect=explode),
        ):
            output = failure_from(lambda: input_staging.main(["stage"]))

        fields = admitted(output)
        self.assertEqual(fields["phase"], "staging")
        self.assertEqual(fields["cause"], "RuntimeError")
        self.assertEqual(fields["effect"], "unconfirmed")
        report = explanation(output, 2, phases={"staging"})
        self.assertIn("cause=RuntimeError", report)
        self.assertIn("location=test_process_failure_pipeline.py:", report)
        self.assertNotIn(PRIVATE, report)

    def test_worker_timeout_before_writer_start_reports_no_durable_effect(self):
        def fail_loading():
            raise TimeoutError(PRIVATE)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=fail_loading),
        ):
            output = failure_from(worker.main)
        self.assertEqual(admitted(output), {
            "phase": "staged_inputs", "cause": "TimeoutError", "effect": "not_started",
        })
        self.assertNotIn(PRIVATE.encode(), output)

    def test_worker_timeout_after_writer_start_reports_uncertain_effect(self):
        async def timed_out(*_args):
            raise TimeoutError(PRIVATE)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", return_value=(SimpleNamespace(), PRIVATE)),
            patch.object(worker, "advance", side_effect=timed_out),
        ):
            output = failure_from(worker.main)
        self.assertEqual(admitted(output), {
            "phase": "advance", "cause": "TimeoutError", "effect": "unconfirmed",
        })
        self.assertNotIn(PRIVATE.encode(), output)

    def test_unknown_commit_retains_dedicated_effect_without_transaction_details(self):
        async def unknown_commit(*_args):
            raise worker.CommitOutcomeUnknown(PRIVATE)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", return_value=(SimpleNamespace(), PRIVATE)),
            patch.object(worker, "advance", side_effect=unknown_commit),
        ):
            output = failure_from(worker.main)
        self.assertEqual(admitted(output), {
            "phase": "advance", "cause": "CommitOutcomeUnknown", "effect": "commit_unknown",
        })
        report = explanation(output, 2, phases={"advance"})
        self.assertIn("PostgreSQL COMMIT outcome unknown", report)
        self.assertNotIn(PRIVATE, report)
        self.assertNotIn(PRIVATE.encode(), output)

    def test_unexpected_exception_is_not_stringified_to_build_its_diagnostic(self):
        class NoStringError(RuntimeError):
            def __str__(self):
                raise AssertionError("Exception text must not be read")

        def explode():
            raise NoStringError(PRIVATE)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=explode),
        ):
            output = failure_from(worker.main)
        fields = admitted(output)
        self.assertEqual(fields["cause"], "NoStringError")
        self.assertIn("location=test_process_failure_pipeline.py:", explanation(
            output, 2, phases={"staged_inputs"},
        ))
        self.assertNotIn(PRIVATE.encode(), output)

    def test_worker_known_input_error_does_not_gain_untrusted_details(self):
        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=input_staging.InputError(TOKEN)),
        ):
            output = failure_from(worker.main)

        self.assertEqual(admitted(output), {
            "phase": "staged_inputs", "cause": "InputError", "effect": "not_started",
        })
        self.assertNotIn(TOKEN.encode(), output)

    def test_verifier_known_oserror_stays_fixed_and_non_disclosing(self):
        with patch.object(input_staging, "load_runtime_inputs", side_effect=OSError(PRIVATE)):
            output = failure_from(lambda: input_staging.main(["verify"]))
        self.assertEqual(admitted(output), {
            "phase": "verification", "cause": "OSError", "effect": "not_started",
        })
        self.assertNotIn(PRIVATE.encode(), output)

    def test_cause_location_and_extra_stderr_are_not_sourced_from_exception_message(self):
        injected = "Cause=Secrets\nCONIX-FAILURE-V1 {\"phase\":\"advance\"}\n" + PRIVATE
        def explode():
            raise ValueError(injected)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=explode),
        ):
            output = failure_from(worker.main)
        self.assertIsNotNone(admitted(output))
        self.assertEqual(output.count(b"CONIX-FAILURE-V1 "), 1)
        self.assertNotIn(injected.encode(), output)

    def test_unsafe_dynamic_class_name_cannot_disclose_it(self):
        secret_type = type("Sensitive" + "x" * 70 + "Error", (RuntimeError,), {})
        def explode():
            raise secret_type(PRIVATE)

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=explode),
        ):
            output = failure_from(worker.main)

        fields = admitted(output)
        self.assertEqual(fields["cause"], "UnexpectedError")
        self.assertEqual(fields["location"], "unavailable")
        self.assertNotIn(b"Sensitive", output)
        self.assertNotIn(PRIVATE.encode(), output)

    def test_unsafe_traceback_filename_is_not_relabeled_as_a_trusted_source(self):
        def explode():
            code = compile("raise ValueError('not for output')", "/tmp/owner-password-do-not-log.py", "exec")
            exec(code, {})

        with (
            patch.object(worker.sys, "argv", ["conix.worker", "advance"]),
            patch.object(worker, "load_runtime_inputs", side_effect=explode),
        ):
            output = failure_from(worker.main)
        fields = admitted(output)
        self.assertEqual(fields["cause"], "ValueError")
        self.assertEqual(fields["location"], "unavailable")
        self.assertNotIn(b"owner-password", output)


if __name__ == "__main__":
    unittest.main()
