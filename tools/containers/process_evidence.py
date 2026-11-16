"""Retain terminal process evidence before an owning lifecycle removes it.

Callers admit the exact container's ownership and control its stop/removal.
This owner reads only selected exit facts and bounded private logs; it grants
no lifecycle authority and never forwards producer prose to the terminal.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from tools.containers.client import OperationError
from tools.containers.diagnostics import PHASES, PREFIX, admitted, error_record, explanation, extract

IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
TIMESTAMP = re.compile(r"[0-9TZ:.+\-]{1,64}\Z")


def _state(record):
    try:
        state = record["State"]
        identity = record["Id"]
        image = record["Image"]
        valid = (IDENTITY.fullmatch(identity) and type(image) is str
                 and re.fullmatch(r"sha256:[0-9a-f]{64}", image)
                 and type(state["Running"]) is bool
                 and type(state["ExitCode"]) is int and 0 <= state["ExitCode"] <= 255
                 and type(state["OOMKilled"]) is bool
                 and type(record["RestartCount"]) is int and record["RestartCount"] >= 0
                 and all(type(state[key]) is str and TIMESTAMP.fullmatch(state[key])
                         for key in ("StartedAt", "FinishedAt")))
        if not valid:
            raise ValueError
        return {"container_id": identity, "image_id": image, "running": state["Running"],
                "exit_code": state["ExitCode"], "oom_killed": state["OOMKilled"],
                "restart_count": record["RestartCount"], "started_at": state["StartedAt"],
                "finished_at": state["FinishedAt"]}
    except (ValueError, KeyError, TypeError) as error:
        raise OperationError("Container exit facts are unavailable; retain its evidence.",
                             diagnostic=error_record(error)) from error


def _private_directory(root):
    for path in (root / "out", root / "out/process-evidence"):
        path.mkdir(mode=0o700, exist_ok=True)
        details = path.lstat()
        if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid()
                or details.st_mode & 0o022):
            raise OperationError("Process evidence needs an owned, non-writable output directory.")
    folder = root / "out/process-evidence"
    folder.chmod(0o700)
    return folder


def _publish(path, content):
    """Publish a private file atomically without following an existing link."""
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
                or details.st_mode & 0o077 or details.st_size != len(content)
                or path.read_bytes() != content):
            raise OperationError("Prior process evidence changed; preserve it and the container.")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".process-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def retain_exit(docker, record):
    """Return admitted terminal facts only after their private receipt is retained."""
    facts = _state(record)
    if facts["running"]:
        raise OperationError("Stop the owned process before retaining its terminal outcome.")
    diagnostic = None
    try:
        stdout, stderr = docker.container_logs(facts["container_id"])
        if (type(stdout) is not bytes or type(stderr) is not bytes
                or len(stdout) + len(stderr) > 65536):
            raise OperationError("Container log capture exceeded its bounded grant.")
        diagnostic = extract(stdout, stderr)
        facts["diagnostic"] = admitted(diagnostic)
        facts["stdout_sha256"] = hashlib.sha256(stdout).hexdigest()
        facts["stderr_sha256"] = hashlib.sha256(stderr).hexdigest()
        folder = _private_directory(Path(docker.working_dir))
        identity = facts["container_id"]
        _publish(folder / (identity + ".stdout"), stdout)
        _publish(folder / (identity + ".stderr"), stderr)
        _publish(folder / (identity + ".json"), (json.dumps(facts, sort_keys=True) + "\n").encode())
    except (OperationError, OSError) as error:
        raise OperationError(
            f"Child exit={facts['exit_code']} observed; diagnostic retention is unconfirmed; container retained.",
            exit_code=facts["exit_code"], diagnostic=diagnostic or error_record(error),
        ) from error
    return facts


def failure(facts, operation):
    """Project retained facts to the established public failure protocol."""
    fields = facts["diagnostic"]
    diagnostic = None if fields is None else PREFIX + json.dumps(fields, separators=(",", ":")).encode()
    detail = explanation(diagnostic, facts["exit_code"], phases=PHASES)
    if detail is None:
        detail = f"exit={facts['exit_code']}; OOM={facts['oom_killed']}; no admitted producer diagnostic; effects unconfirmed"
    return OperationError(f"{operation} stopped: {detail}.", exit_code=facts["exit_code"], diagnostic=diagnostic)


def require_running(docker, record, operation):
    """A stopped owned service reports its retained producer outcome, not identity drift."""
    if type(record.get("State", {}).get("Running")) is not bool:
        raise OperationError("Owned process running state is unavailable.")
    if not record["State"]["Running"]:
        raise failure(retain_exit(docker, record), operation)


def preserve_primary(primary, facts, operation):
    """Expose a recovered child outcome while keeping an earlier failure primary."""
    if facts is None or not facts["exit_code"] and facts["diagnostic"] is None:
        return primary
    observed = failure(facts, operation)
    if primary is None:
        return observed
    try:
        print("Retained child outcome: " + str(observed), file=sys.stderr)
    except OSError:
        pass
    if isinstance(primary, OperationError) and primary.diagnostic is None:
        primary.diagnostic = error_record(observed)
        primary.exit_code = primary.exit_code if primary.exit_code is not None else observed.exit_code
    return primary
