"""Read fixed process-failure records, never arbitrary subprocess stderr.

The installed producer controls three literal identifiers. A malformed,
oversized, unknown or multiline record is rejected wholesale without
attempted redaction or copying source exception prose.
"""

import json

PREFIX = b"CONIX-FAILURE-V1 "
PHASES = frozenset({"staging", "verification", "staged_inputs", "advance"})
CAUSES = frozenset({
    "InputError", "OSError", "UnexpectedError", "CommitOutcomeUnknown",
    "BranchConflict", "HistoryConflict", "StorageUnavailable", "WriterBusy",
    "RpcProtocolError", "RpcUnavailable", "MalformedABIData",
    "ResourceExceeded", "TimeoutError", "KeyboardInterrupt",
})
EFFECTS = frozenset({"not_started", "unconfirmed", "commit_unknown"})


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
    if (type(fields) is not dict or set(fields) != {"phase", "cause", "effect"}
            or any(type(value) is not str for value in fields.values())
            or fields["phase"] not in PHASES
            or fields["cause"] not in CAUSES
            or fields["effect"] not in EFFECTS
            or (fields["effect"] == "commit_unknown") != (fields["cause"] == "CommitOutcomeUnknown")):
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
    return f"phase={phase}; cause={fields['cause']}; effect={effect}; exit={exit_code}"
