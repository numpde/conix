"""Networkless target-CPython selection from the shared NMR wheel pool.

The package installer is exercised in --dry-run mode in a contained Python
worker; actual installation happens only in a separate BuildKit image stage.
The released tool projects the successful pip report to exact stream inputs.
"""
import argparse
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path


class MissingSelection(Exception):
    """The target Python reported an absent required distribution in the pool."""


def run(pool, release, runtime, build):
    """Select exact locked wheels offline; exit 10 only for missing artifacts."""
    pin = json.loads(release.read_text())
    tool = pool / ("wheelhouse-tool-" + pin["version"] + ".pyz")
    if sha256(tool.read_bytes()).hexdigest() != pin["sha256"]:
        raise ValueError("Contained shared-wheelhouse tool differs from the pinned upstream release.")

    # Reserve an exclusive report path: pip must not overwrite a stale
    # selection from an earlier attempt.
    with Path("/tmp/wheelhouse-pip.json").open("xb"):
        pass

    argv = [
        sys.executable, "-I", "-m", "pip", "--isolated", "install",
        "--dry-run", "--ignore-installed", "--quiet",
        "--no-index", "--no-cache-dir", "--disable-pip-version-check",
        "--no-deps", "--require-hashes", "--only-binary=:all:",
        "--find-links", str(pool), "--report", "/tmp/wheelhouse-pip.json",
        "-r", str(runtime), "-r", str(build),
    ]
    pip_selection = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
    if pip_selection.returncode:
        # Missing is the ONLY offline status that permits explicit acquisition.
        # Hash mismatches, invalid locks, file permission errors and other
        # installer failures must never escalate to a network-enabled worker.
        explanation = pip_selection.stderr.decode("utf-8", errors="replace")
        if (
            "No matching distribution found" in explanation
            and "THESE PACKAGES DO NOT MATCH THE HASHES" not in explanation
        ):
            print("Missing one or more target-compatible locked wheels.", file=sys.stderr)
            raise MissingSelection()

        raise ValueError(
            "The selected target interpreter rejected cached wheel integrity "
            "or requirements; do not start network acquisition."
        )

    manifest_conversion = subprocess.run(
        [
            sys.executable, "-I", str(tool), "report",
            "--report", "/tmp/wheelhouse-pip.json",
            "--pool", str(pool), "--directory", "wheelhouse",
        ],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
    )
    if manifest_conversion.returncode:
        raise ValueError("Pinned wheelhouse-tool refused the contained pip selection report.")

    artifacts = json.loads(manifest_conversion.stdout)
    if type(artifacts) is not list or not artifacts:
        raise ValueError("The contained selector returned no wheel artifact identities.")

    print(json.dumps(artifacts, sort_keys=True, separators=(",", ":")))


def main():
    """Keep missing-wheels distinct from integrity and tool failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()

    try:
        run(args.pool, args.release, args.runtime, args.build)
    except MissingSelection:
        return 10
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"Shared wheelhouse offline selection refused: {type(error).__name__}.", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
