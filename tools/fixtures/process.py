"""Bound fixture child processes and keep captured output private.

Both real-chain and retained-deployment campaigns use this owner for deadlines,
progress, private stdin and process-group retirement. Docker resource ownership
and cleanup remain with the calling campaign.
"""

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


def owned_command(argv, *, output, error_file, env, timeout, payload=None, label="fixture command"):
    """Bound and reap an owned child while keeping private receipts off terminal."""
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
        raise CampaignError(f"{label} exited with status {owned_process.returncode}; see private {error_file.name}.")

    if output.stat().st_size > 1048576:
        raise CampaignError(f"{label} produced an oversized receipt; no JSON was admitted.")

    progress(f"{label}: completed ({int(time.monotonic() - started_at)}s)")

    return output.read_bytes()
