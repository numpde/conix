"""Initialize only the two new replay volumes under a narrow CHOWN grant."""
import os
from pathlib import Path
import stat
import sys


def main():
    if sys.argv[1:] != ["init"]:
        raise SystemExit("Unknown replay volume operation.")
    for name in ("evidence", "results"):
        path = Path("/init") / name
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or any(path.iterdir()):
            raise SystemExit("Replay volume is not empty.")
        os.chmod(path, 0o700)  # Root owns the volume until the handoff.
        os.chown(path, 65532, 65532)


if __name__ == "__main__":
    main()
