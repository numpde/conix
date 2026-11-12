"""Emit a bounded, source-controlled process failure record on stderr.

Records contain only reviewed phase/cause/effect identifiers, never exception
text or untrusted RPC/SQL/configuration values. These are not application logs.
"""

import json
import sys

PREFIX = "CONIX-FAILURE-V1 "


def emit(*, phase: str, cause: str, effect: str) -> None:
    """Publish one fixed-shape diagnostic, usable after an unsuccessful exit."""
    fields = {"phase": phase, "cause": cause, "effect": effect}
    line = PREFIX + json.dumps(fields, separators=(",", ":"), ensure_ascii=True)
    print(line, file=sys.stderr, flush=True)
