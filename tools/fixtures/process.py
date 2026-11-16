"""Bound fixture child processes and keep captured output private.

Both real-chain and retained-deployment campaigns use this owner for deadlines,
progress, private stdin and process-group retirement. Docker resource ownership
and cleanup remain with the calling campaign.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


class CampaignError(Exception):
    """A fixture operation or its completion could not be established."""

    def __init__(self, detail, *, exit_code=None, diagnostic=None):
        super().__init__(detail)
        self.exit_code = exit_code
        self.diagnostic = diagnostic


@contextmanager
def cancellation():
    """Route host SIGTERM through child retirement and owned-resource cleanup."""
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def clean_environment():
    """Exclude ambient Docker, Make, Compose and private-wallet authority."""
    excluded_prefixes = (
        "DOCKER_", "COMPOSE_", "BUILDX_", "CONIX_MAKE_", "CAM_",
        "PAY_TO_PLAY_", "MAKE", "GNUMAKEFLAGS",
    )
    excluded_names = ("PRIVATE_KEY", "BASH_ENV", "ENV", "FIXTURE_ID", "SCENARIO")

    return {
        name: value for (name, value) in os.environ.items()
        if not name.startswith(excluded_prefixes) and name not in excluded_names
    }


def progress(message):
    """Emit only source-controlled progress labels, never child output."""
    print(f"[conix] {message}", file=sys.stderr, flush=True)


def retire_deployment(make, docker, name, records):
    """A reported child failure and confirmed container removal are separate facts."""
    from tools.containers.client import OperationError
    from tools.containers.deployment import _candidate_ids
    from tools.containers.diagnostics import admitted, error_record

    try:
        make("deployment/down")
    except (CampaignError, OperationError) as error:
        records.setdefault("consumer_failure", {
            "diagnostic": admitted(error_record(error)), "exit_code": error.exit_code,
        })
        observed = retirement_failure(records)
        try:
            progress(str(observed))
        except OSError:
            pass
    if _candidate_ids(docker, "conix-" + name):
        raise OperationError("Owned consumer retirement is unconfirmed; retain its dependencies.")


def retirement_failure(records):
    """Recover a producer failure reported during otherwise successful teardown."""
    from tools.containers.client import OperationError
    from tools.containers.diagnostics import PHASES, PREFIX, explanation

    record = records.get("consumer_failure")
    if record is None:
        return None
    diagnostic = PREFIX + json.dumps(record["diagnostic"]).encode("ascii")
    detail = explanation(diagnostic, record["exit_code"], phases=PHASES)
    return OperationError("Consumer failed before retirement: " + detail,
                          exit_code=record["exit_code"], diagnostic=diagnostic)


def owned_command(argv, *, output, error_file, env, timeout, payload=None, label="fixture command",
                  binary_output_limit=None):
    """Bound and reap a child; an explicit binary grant retains its file only.

    Ordinary callers still receive a bounded JSON/control receipt. A binary
    producer must itself enforce its physical write limit while running;
    this owner checks the completed file and never loads it into memory.
    """
    if binary_output_limit is not None and (type(binary_output_limit) is not int or binary_output_limit <= 0):
        raise ValueError("Binary process output requires a positive byte grant.")
    started_at = time.monotonic()
    progress(f"{label}: started")

    with output.open("wb") as stdout_file, error_file.open("wb") as stderr_file:
        output.chmod(0o600)
        error_file.chmod(0o600)

        owned_process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )

        payload_pending = True

        try:
            while True:
                remaining_seconds = timeout - (time.monotonic() - started_at)

                if remaining_seconds <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)

                try:
                    owned_process.communicate(
                        input=payload if payload_pending else None,
                        timeout=min(15, remaining_seconds),
                    )
                    break
                except subprocess.TimeoutExpired:
                    # Resume the same child without resending its private stdin.
                    payload_pending = False

                    if time.monotonic() - started_at >= timeout:
                        raise

                    progress(f"{label}: running ({int(time.monotonic() - started_at)}s)")
        except BaseException as error:
            # Preserve the original failure even when the child ignores both
            # signals. A stopped CLI still does not prove Docker daemon work
            # was retired; the campaign independently verifies Compose cleanup.
            child_retirement_unconfirmed = False

            try:
                os.killpg(owned_process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                child_retirement_unconfirmed = True

            try:
                owned_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(owned_process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    child_retirement_unconfirmed = True

                try:
                    owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    child_retirement_unconfirmed = True
            except OSError:
                child_retirement_unconfirmed = True

            if child_retirement_unconfirmed:
                error.add_note(
                    "Fixture child retirement is unconfirmed; inspect the "
                    "selected operation before deleting retained evidence."
                )

            if isinstance(error, subprocess.TimeoutExpired):
                retirement = "; child retirement unconfirmed" if child_retirement_unconfirmed else ""
                raise CampaignError(
                    f"{label} timed out after {timeout}s; daemon effects are unconfirmed{retirement}."
                ) from error

            raise

    if owned_process.returncode != 0:
        from tools.containers.diagnostics import PHASES, explanation, extract

        with error_file.open("rb") as errors, output.open("rb") as results:
            streams = [errors.read(65537)]
            if binary_output_limit is None:
                streams.append(results.read(65537))
            diagnostic = extract(*streams)
        reason = explanation(diagnostic, owned_process.returncode, phases=PHASES)
        detail = reason or f"exit={owned_process.returncode}; no admitted producer diagnostic; effects unconfirmed"
        raise CampaignError(f"{label} failed: {detail}", exit_code=owned_process.returncode,
                            diagnostic=diagnostic)

    output_limit = 1048576 if binary_output_limit is None else binary_output_limit
    if output.stat().st_size > output_limit:
        raise CampaignError(f"{label} exceeded its output byte grant; private output retained.")

    progress(f"{label}: completed ({int(time.monotonic() - started_at)}s)")

    if binary_output_limit is None:
        return output.read_bytes()
