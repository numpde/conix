"""Small private control-plane files for a disposable recorded stress capture.

No public source payload or WARC bytes are returned through Docker stdout.
"""

import json
import os
from pathlib import Path
import sys
import time

CAPTURE = Path("/capture")


def write_record(name, document):
    """Atomically publish a bounded, owner-private control record."""
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(raw) > 1048576:
        raise ValueError("Capture control record exceeds its limit.")
    target = CAPTURE / name
    temporary = CAPTURE / (name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("clock", "seed", "release"):
        raise SystemExit("Select clock, seed or release.")
    action = sys.argv[1]
    if action == "clock":
        print(time.monotonic_ns())
    elif action == "seed":
        path = CAPTURE / "seed.json"
        raw = path.read_bytes()
        if not 0 < len(raw) <= 4096:
            raise SystemExit("Seed record is unavailable.")
        print(json.dumps(json.loads(raw), separators=(",", ":")))
    else:
        # The observer records its own actual release observation.
        descriptor = os.open(CAPTURE / "release", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)


if __name__ == "__main__":
    main()
