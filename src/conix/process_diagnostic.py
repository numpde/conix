"""Emit bounded, source-controlled process failure records on stderr.

Known failures report fixed phase/cause/effect identifiers. Unexpected failures
also report the actual safe exception class name and traceback basename:line,
or 'unavailable' if that metadata cannot be admitted. Never copy exception
messages, absolute filesystem paths, or untrusted RPC/SQL/configuration data.
"""

import json
from pathlib import Path
import re
import sys

PREFIX = "CONIX-FAILURE-V1 "


def emit(*, phase: str, cause: str, effect: str, location: str | None = None) -> None:
    """Publish one bounded failure record; source location is optional."""
    fields = {"phase": phase, "cause": cause, "effect": effect}
    if location is not None:
        fields["location"] = location
    line = PREFIX + json.dumps(fields, separators=(",", ":"), ensure_ascii=True)
    print(line, file=sys.stderr, flush=True)


# Only source-derived Python identifiers and a basename:line are eligible for
# stderr. Neither exception messages nor absolute filesystem paths are copied.
TYPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
SOURCE_LOCATION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\.py:[1-9][0-9]{0,5}\Z")


def emit_unexpected(*, phase: str, error: Exception, effect: str) -> None:
    """Report the actual safe exception type and last traceback source point."""
    cause = type(error).__name__
    if TYPE_NAME.fullmatch(cause) is None:
        emit(phase=phase, cause="UnexpectedError", effect=effect, location="unavailable")
        return

    location = "unavailable"
    current = error.__traceback__
    while current is not None:
        filename = Path(current.tb_frame.f_code.co_filename).name
        candidate = f"{filename}:{current.tb_lineno}"
        location = candidate if SOURCE_LOCATION.fullmatch(candidate) is not None else "unavailable"
        current = current.tb_next

    emit(phase=phase, cause=cause, effect=effect, location=location)
