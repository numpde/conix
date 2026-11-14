"""Admit bounded process-failure records, never arbitrary subprocess stderr.

Three fixed fields identify phase, cause and durable effect. Unexpected errors
may add a narrowly admitted exception-type identifier and source basename:line.
Malformed, oversized, unexpected-field or multiline records are rejected
without redacting or forwarding exception messages.
"""

import json
import re

PREFIX = b"CONIX-FAILURE-V1 "
PHASES = frozenset({"staging", "verification", "staged_inputs", "advance", "reader_check"})
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
    if type(fields) is not dict or set(fields) not in (
        {"phase", "cause", "effect"},
        {"phase", "cause", "effect", "location"},
    ):
        return None
    if any(type(value) is not str for value in fields.values()):
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
    return f"phase={phase}; cause={fields['cause']}; effect={effect}{location}; exit={exit_code}"
