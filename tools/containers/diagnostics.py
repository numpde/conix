"""Admit bounded process-failure records, never arbitrary subprocess stderr.

Three fixed fields identify phase, cause and durable effect. Unexpected errors
may add a narrowly admitted exception-type identifier and source basename:line.
Malformed, oversized, unexpected-field or multiline records are rejected
without redacting or forwarding exception messages.
"""

import json
import re
from pathlib import Path
import traceback

PREFIX = b"CONIX-FAILURE-V1 "
PHASES = frozenset({"staging", "verification", "staged_inputs", "advance", "reader_check", "operator", "schema_init", "network_measure", "run", "history_inspect", "history_repair", "wheel_selection"})
CAUSES = frozenset({
    "InputError", "OSError", "CommitOutcomeUnknown",
    "BranchConflict", "HistoryConflict", "StorageUnavailable", "WriterBusy",
    "RpcProtocolError", "RpcUnavailable", "MalformedABIData",
    "ResourceExceeded", "TimeoutError", "KeyboardInterrupt",
})
EFFECTS = frozenset({"not_started", "unconfirmed", "commit_unknown"})
CAUSE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
SOURCE_LOCATION = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]{0,63}\.py:[1-9][0-9]{0,5}|unavailable)\Z")


def _unique_pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate diagnostic field.")
    return result


def admitted(stderr):
    """Return exact controlled fields, or None without exposing stderr."""
    if type(stderr) is not bytes or len(stderr) > 320 or not stderr.startswith(PREFIX):
        return None
    payload = stderr[len(PREFIX):].removesuffix(b"\n")
    if b"\n" in payload or b"\r" in payload:
        return None
    try:
        fields = json.loads(payload, object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError):
        return None
    required = {"phase", "cause", "effect"}
    if (type(fields) is not dict or not required <= fields.keys()
            or fields.keys() - required - {"location", "child_exit"}):
        return None
    if any(type(value) is not str for key, value in fields.items() if key != "child_exit"):
        return None
    if "child_exit" in fields and (type(fields["child_exit"]) is not int
                                    or not -255 <= fields["child_exit"] <= 255):
        return None

    location = fields.get("location")
    if (
        fields["phase"] not in PHASES
        or fields["effect"] not in EFFECTS
        or (fields["effect"] == "commit_unknown") != (fields["cause"] == "CommitOutcomeUnknown")
        or (location is None and fields["cause"] not in CAUSES)
        or (location is not None and (
            SOURCE_LOCATION.fullmatch(location) is None
            or CAUSE_NAME.fullmatch(fields["cause"]) is None
        ))
    ):
        return None
    return fields


def explanation(stderr, exit_code, *, phases):
    """Build one safe fixed message retaining phase, cause, effect and exit."""
    fields = admitted(stderr)
    if fields is None or fields["phase"] not in phases:
        return None
    phase = fields["phase"].replace("_", " ")
    effect = {
        "not_started": "operation did not start",
        "unconfirmed": "durable outcome unconfirmed",
        "commit_unknown": "PostgreSQL COMMIT outcome unknown",
    }[fields["effect"]]
    location = f"; location={fields['location']}" if "location" in fields else ""
    child = f"; child_exit={fields['child_exit']}" if "child_exit" in fields else ""
    return f"phase={phase}; cause={fields['cause']}; effect={effect}{location}; exit={exit_code}{child}"


def extract(*outputs):
    """Select one admitted producer record without forwarding surrounding prose."""
    if any(type(output) is not bytes for output in outputs) or sum(map(len, outputs)) > 65536:
        return None
    records = [line for output in outputs for line in output.splitlines() if line.startswith(PREFIX)]
    if len(records) != 1 or admitted(records[0]) is None:
        return None
    return records[0]


def error_record(error):
    """Preserve an admitted producer record or describe the host failure safely."""
    fields = admitted(getattr(error, "diagnostic", None))
    if fields is None:
        category = type(error).__name__
        category = category if CAUSE_NAME.fullmatch(category) else "UnexpectedError"
        frames = traceback.extract_tb(error.__traceback__)
        location = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "unavailable"
        if SOURCE_LOCATION.fullmatch(location) is None:
            location = "unavailable"
        fields = {"phase": "operator", "cause": category, "effect": "unconfirmed", "location": location}
    exit_code = getattr(error, "exit_code", None)
    if type(exit_code) is int and -255 <= exit_code <= 255:
        # A new process boundary has its own exit status. Keep the innermost
        # observed child status when Make or Docker adds another outer status.
        fields.setdefault("child_exit", exit_code)
    # Re-emit checked fields: accepted JSON encodings need not themselves be
    # ASCII, while the operator terminal boundary deliberately is.
    return PREFIX + json.dumps(fields, separators=(",", ":")).encode("ascii")
