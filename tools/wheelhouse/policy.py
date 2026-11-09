"""Host-owned shared wheel pool contract, from NMR wheelhouse-tool v0.5.14.

This module never installs packages on the host. Target pip selection and
acquisition run in restricted containers on the selected Docker Engine. The
released standalone Python zipapp streams verified wheel bytes to BuildKit;
the host process only coordinates the stream and source admission.
"""

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
WHEEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\.whl\Z")
TEST_SOURCES = (
    "tests/test_tier0.py",
    "tests/test_input_staging.py",
    "tests/prepare_schema.py",
    "tests/fixture_protocol.py",
    "tests/pay_to_play_integration.py",
)


class PoolError(ValueError):
    """A pool, released tool, source or wheel identity is not admitted."""


def admit_pool(value):
    """Select a *pre-existing* owner-controlled directory, never silently default.

    Reject every symlink ancestor before resolving to prevent mutable path
    redirection into another project or Docker bind source.
    """
    if type(value) is not str or not value or any(ch in value for ch in (",", "\n", "\r", "\x00")):
        raise PoolError("WHEELHOUSE must explicitly select an absolute path without bind delimiters.")
    path = Path(value)
    if not path.is_absolute() or str(path) == "/":
        raise PoolError("WHEELHOUSE must identify an existing directory below /.")
    ancestor = path
    while True:
        if ancestor.is_symlink():
            raise PoolError("WHEELHOUSE must not contain a symlink ancestor.")
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    try:
        info = path.stat()
    except OSError as error:
        raise PoolError("The selected WHEELHOUSE directory does not exist or cannot be inspected.") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or info.st_mode & 0o022 or not info.st_mode & stat.S_IRUSR
        or not info.st_mode & stat.S_IWUSR or not info.st_mode & stat.S_IXUSR):
        raise PoolError("WHEELHOUSE must be an owner-readable/writable directory without group/other write.")
    return path


def release_record():
    """Pin the already-published NMR tool; no independent compatibility logic."""
    try:
        document = json.loads((ROOT / "requirements/wheelhouse-tool-release.json").read_text())
    except (OSError, ValueError) as error:
        raise PoolError("Cannot read the reviewed shared wheelhouse-tool release record.") from None
    if (type(document) is not dict or set(document) != {"version", "sha256"}
        or type(document["version"]) is not str or VERSION.fullmatch(document["version"]) is None
        or type(document["sha256"]) is not str or SHA.fullmatch(document["sha256"]) is None):
        raise PoolError("The wheelhouse-tool pin must specify one semantic version and full SHA-256.")
    return document | {"name": f"wheelhouse-tool-{document['version']}.pyz"}


def has_tool(path, pin):
    """Reuse a verified released bundle or refuse unexpected existing bytes."""
    name = pin["name"]
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return False
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024 * 1024:
                raise PoolError("Existing shared wheelhouse tool must be a regular file no larger than 16 MiB.")
            if sha256(stream.read()).hexdigest() != pin["sha256"]:
                raise PoolError("Existing shared wheelhouse tool bytes differ from the pinned release; preserved.")
            return True
    finally:
        os.close(directory)


def source_allowlist(root=ROOT):
    """Capture only Dockerfile inputs, with exact hashes and executable flags."""
    relative = (
        "Dockerfile", "pyproject.toml", "requirements/runtime.lock",
        "requirements/build.lock", *TEST_SOURCES,
    )
    src = root / "src" / "conix"
    modules = tuple(sorted(file.relative_to(root).as_posix() for file in src.rglob("*.py")))
    if not modules or "src/conix/__init__.py" not in modules:
        raise PoolError("Conix source package is absent from the selected build context.")
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--cached", "-z", "--", "src/conix"],
            cwd=root, capture_output=True, timeout=10, check=True,
        ).stdout.decode("utf-8").split("\0")
    except (OSError, UnicodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise PoolError("Cannot establish the declared Git source inventory.") from None
    declared = {name for name in tracked if name}
    if declared != set(modules):
        raise PoolError(
            "Source package differs from tracked Git inputs; do not stream "
            "unreviewed or untracked Python files into a build."
        )
    paths = (*relative, *modules)
    if len(paths) != len(set(paths)):
        raise PoolError("The curated image source list contains duplicates.")
    entries = []
    for name in paths:
        path = root / name
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise PoolError("A selected image source is beneath a symlink.")
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                raise PoolError("Image inputs must be bounded regular files.")
            content = path.read_bytes()
        except OSError:
            raise PoolError("Cannot capture a required image source.") from None
        entries.append({
            "path": str(path),
            "destination": name,
            "sha256": sha256(content).hexdigest(),
            "executable": bool(info.st_mode & 0o111),
        })
    return entries


def project_issuer_lock(source):
    """Pure, lossless syntax projection for the released NMR metadata locator.

    Conix already pins one exact wheel SHA per requirement on a single line.
    NMR's released locator expects a separate --hash line. This is NOT a
    dependency resolver or a second committed lock; the acquisition tool
    binds its ephemeral pylock to this exact normalized source.
    """
    if type(source) is not str or not source or len(source) > 1024 * 1024:
        raise PoolError("Cannot project an empty or oversized issuer requirements lock.")
    records = []
    seen = set()
    expression = re.compile(
        r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)"
        r" --hash=sha256:([0-9a-f]{64})\Z"
    )
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        found = expression.fullmatch(line)
        if found is None:
            raise PoolError("Cannot project issuer lock: exact version and SHA-256 required.")
        name, version, digest = found.groups()
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if canonical in seen:
            raise PoolError("Cannot project issuer lock: repeated package identity.")
        seen.add(canonical)
        records.append(f"{name}=={version} \\\n    --hash=sha256:{digest}\n")
    if not records:
        raise PoolError("Cannot project an empty issuer requirements lock.")
    return "".join(records)


def selection_manifest(raw):
    """Check the target-interpreter's hash-verified pip selection report."""
    if type(raw) is not bytes or not raw or len(raw) > 1024 * 1024:
        raise PoolError("The selected-wheel manifest is empty or oversized.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise PoolError("The contained selector returned invalid JSON.") from None
    if type(payload) is not list or not payload or len(payload) > 256:
        raise PoolError("The selected-wheel manifest must contain a bounded artifact list.")
    seen = set()
    for item in payload:
        if (type(item) is not dict or set(item) != {"directory", "filename", "sha256"}
            or item["directory"] != "wheelhouse"
            or type(item["filename"]) is not str or WHEEL.fullmatch(item["filename"]) is None
            or type(item["sha256"]) is not str or SHA.fullmatch(item["sha256"]) is None
            or item["filename"] in seen):
            raise PoolError("The selected-wheel manifest contains an unapproved or repeated artifact.")
        seen.add(item["filename"])
    return sorted(payload, key=lambda item: item["filename"])


def worker_grants(pool, *, user, network=False, write=None):
    """Minimum Docker run grants for one target-interpreter worker.

    On a qualified rootless daemon, container uid 0 maps to the invoking
    unprivileged host account. A rootful CI engine instead uses the host
    account's numeric uid/gid. Neither worker receives Docker or host sockets.
    """
    if write is None:
        write = network
    if write and not network:
        raise PoolError("Shared wheelhouse cannot be modified by a networkless selector.")
    return (
        "--rm", "--init", "--pull", "never",
        "--network", "bridge" if network else "none",
        "--read-only", "--user", user, "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "64", "--memory", "384m",
        "--cpus", "1",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777,size=128m",
        "--mount", f"type=bind,src={pool},dst=/wheelhouse" + ("" if write else ",readonly"),
    )
