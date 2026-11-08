"""Networkless target-CPython selection from the shared NMR wheel pool.

The package installer is exercised in --dry-run mode in a contained Python
worker; actual installation happens only in a separate BuildKit image stage.
The released tool projects the successful pip report to exact stream inputs.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys


def run(pool, release, runtime, build):
    pin = json.loads(release.read_text())
    tool = pool / ("wheelhouse-tool-" + pin["version"] + ".pyz")
    if sha256(tool.read_bytes()).hexdigest() != pin["sha256"]:
        raise ValueError("Contained shared-wheelhouse tool differs from the pinned upstream release.")
    with Path("/tmp/wheelhouse-pip.json").open("xb") as report:
        pass
    argv = [
        sys.executable, "-I", "-m", "pip", "--isolated", "install",
        "--dry-run", "--ignore-installed", "--quiet",
        "--no-index", "--no-cache-dir", "--disable-pip-version-check",
        "--no-deps", "--require-hashes", "--only-binary=:all:",
        "--find-links", str(pool), "--report", "/tmp/wheelhouse-pip.json",
        "-r", str(runtime), "-r", str(build),
    ]
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError(
            "The selected target interpreter could not satisfy both original "
            "hash-locked requirements from the shared pool."
        )
    converted = subprocess.run(
        [
            sys.executable, "-I", str(tool), "report",
            "--report", "/tmp/wheelhouse-pip.json",
            "--pool", str(pool), "--directory", "wheelhouse",
        ],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
    )
    if converted.returncode:
        raise ValueError("Pinned wheelhouse-tool refused the contained pip selection report.")
    result = json.loads(converted.stdout)
    if type(result) is not list or not result:
        raise ValueError("The contained selector returned no wheel artifact identities.")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.pool, args.release, args.runtime, args.build)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"Shared wheelhouse offline selection refused: {type(error).__name__}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
