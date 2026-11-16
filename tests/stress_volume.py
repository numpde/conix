"""Initialize only the two new capture volumes; never handle host paths."""

import os
from pathlib import Path
import stat
import sys


def main():
    if sys.argv[1:] != ["init"]:
        raise SystemExit("Unknown capture volume operation.")
    for name in ("application", "observer"):
        path = Path("/init") / name
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or any(path.iterdir()):
            raise SystemExit("Capture volume is not a fresh directory.")
        os.chown(path, 65532, 65532)
        os.chmod(path, 0o700)


if __name__ == "__main__":
    main()
