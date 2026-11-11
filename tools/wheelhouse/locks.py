"""Explicitly regenerate wheel-hash locks for Linux/amd64 and CPython 3.12.

Networked acquisition is an operator action, separate from image packaging.
The lock records exactly one binary artifact per pinned package for this target.
"""
import argparse
import base64
import os
import platform
import subprocess
import sys
import tempfile
import tomllib
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
def selected_target():
    """Refuse generating a lock for the wrong Python/platform tag set."""
    if (sys.version_info[:2] != (3, 12) or
            platform.system() != "Linux" or platform.machine() != "x86_64"):
        raise ValueError("Lock regeneration requires CPython 3.12 on Linux x86_64.")


def inventory(requirements, temp: Path, label: str) -> str:
    """Resolve once, then pin one exact wheel digest per package identity."""
    request = temp / (label + ".in")
    request.write_text("\n".join(requirements) + "\n", encoding="utf-8")

    wheels = temp / label
    wheels.mkdir()
    subprocess.run([
        sys.executable, "-m", "pip", "--isolated", "download",
        "--only-binary=:all:", "--dest", str(wheels), "--requirement", str(request),
    ], check=True)

    files = sorted(wheels.iterdir())
    if not files or any(path.suffix != ".whl" for path in files):
        raise ValueError("Dependency resolution did not yield only binary wheel files.")

    entries = []
    seen = set()
    for path in files:
        parts = path.name[:-4].split("-")
        if len(parts) < 5:
            raise ValueError(f"Invalid wheel name in selected {label} closure.")

        (name, version) = parts[:2]
        project = name.lower().replace("_", "-")
        if project in seen:
            raise ValueError(f"Two selected wheels for {project}; an exact lock needs one.")

        seen.add(project)
        digest = sha256(path.read_bytes()).hexdigest()
        entries.append(f"{project}=={version} --hash=sha256:{digest}")

    header = [
        "# Target: CPython 3.12 / Linux x86_64, binary wheels only.",
        "# Regenerate explicitly with: python3 -m tools.wheelhouse.locks --write",
        "# Acquisition may use the network. Runtime image build MUST use no-index.",
    ]

    return "\n".join(header + sorted(entries) + [""])


def generate():
    """Produce runtime and build locks from their independent root sets."""
    selected_target()
    with (ROOT / "pyproject.toml").open("rb") as stream:
        source = tomllib.load(stream)

    runtime_requirements = source["project"]["dependencies"]
    build_requirements = source["build-system"]["requires"]
    for (name, requirements) in (
        ("runtime", runtime_requirements),
        ("build", build_requirements),
    ):
        if type(requirements) is not list or any(type(item) is not str for item in requirements):
            raise ValueError(f"Project {name} requirements must be a concrete list.")

    with tempfile.TemporaryDirectory(prefix="conix-lock-") as temp:
        working = Path(temp)
        return {
            "runtime": inventory(runtime_requirements, working, "runtime"),
            "build": inventory(build_requirements, working, "build"),
        }


def main():
    """Regenerate only when explicitly requested; regular builds stay offline."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--emit-b64", action="store_true")
    args = parser.parse_args()

    locks = generate()
    if args.write:
        for (name, content) in locks.items():
            destination = ROOT / "requirements" / (name + ".lock")
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            print(f"Wrote {destination}")
    else:
        for (name, content) in locks.items():
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            print(f"CONIX_{name.upper()}_LOCK_B64={encoded}")


if __name__ == "__main__":
    main()
