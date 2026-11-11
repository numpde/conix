"""Provision fixed private inputs once, through an isolated stdin-only process.

The operator runs this module as UID 0 with CAP_CHOWN, no network and a
read-only root filesystem. It passes ownership to the restricted runtime UID
before the worker receives the volume read-only.
"""

import os
import re
import stat
import struct
import sys
import traceback
from itertools import islice
from pathlib import Path

MAGIC = b"CONIX-INPUTS-V2\n"
MAX_CONFIG = 64 * 1024
MAX_DSN = 8192
MAX_ABI = 1024 * 1024
MAX_ABI_TOTAL = 8 * MAX_ABI
MAX_ABI_FILES = 32
MAX_ABI_NAME = 128
MAX_ENVELOPE = len(MAGIC) + 12 + MAX_CONFIG + MAX_DSN + MAX_ABI_TOTAL + MAX_ABI_FILES * (6 + MAX_ABI_NAME)
TARGET = Path("/run/conix")
CONFIG = "config.toml"
DSN = "connection.dsn"
OWNER = 65532


class InputError(ValueError):
    """A source-controlled diagnostic that cannot contain credential bytes."""


def decode(payload: bytes) -> tuple[bytes, bytes, dict[str, bytes]]:
    """Split bounded transport bytes without interpreting catalog settings.

    The catalog is the only owner of TOML/ABI admission. This networkless
    provisioner handles private file delivery and does not authorize a worker.
    """
    header_size = len(MAGIC) + 12
    if not payload.startswith(MAGIC) or not header_size <= len(payload) <= MAX_ENVELOPE:
        raise InputError("Staged input envelope has an invalid version or length.")

    (config_size, dsn_size, count) = struct.unpack_from("!III", payload, len(MAGIC))

    if not 0 < config_size <= MAX_CONFIG or not 0 < dsn_size <= MAX_DSN:
        raise InputError("Staged configuration or credential input exceeds its bound.")
    if not 1 <= count <= MAX_ABI_FILES:
        raise InputError("Staged ABI file count is outside its bound.")

    dsn_offset = header_size + config_size
    abi_offset = dsn_offset + dsn_size
    if abi_offset > len(payload):
        raise InputError("Staged inputs were truncated or have trailing bytes.")

    config_bytes = payload[header_size:dsn_offset]
    dsn_bytes = payload[dsn_offset:abi_offset]

    try:
        dsn_text = dsn_bytes.decode("utf-8").strip()
    except UnicodeError as error:
        raise InputError("Staged credential bytes must be UTF-8.") from error

    if not dsn_text or "\x00" in dsn_text:
        raise InputError("Staged credential file is empty or contains a NUL byte.")

    return (config_bytes, dsn_bytes, _decode_abis(payload, abi_offset, count))


def _decode_abis(payload: bytes, offset: int, count: int) -> dict[str, bytes]:
    members = {}
    remaining = MAX_ABI_TOTAL
    for _ in range(count):
        if offset + 6 > len(payload):
            raise InputError("Staged inputs were truncated or have trailing bytes.")
        (name_size, content_size) = struct.unpack_from("!HI", payload, offset)
        name_start = offset + 6
        content_start = name_start + name_size
        offset = content_start + content_size
        if not 1 <= name_size <= MAX_ABI_NAME or not 1 <= content_size <= min(MAX_ABI, remaining):
            raise InputError("Staged ABI file exceeds its bound.")
        if offset > len(payload):
            raise InputError("Staged inputs were truncated or have trailing bytes.")

        name_bytes = payload[name_start:content_start]
        if not re.fullmatch(rb"[A-Za-z0-9][A-Za-z0-9_.-]{0,122}\.json", name_bytes):
            raise InputError("Staged ABI file name is invalid.")
        name = name_bytes.decode("ascii")
        if name in members:
            raise InputError("Staged ABI file name is duplicated.")
        members[name] = payload[content_start:offset]
        remaining -= content_size

    if offset != len(payload):
        raise InputError("Staged inputs were truncated or have trailing bytes.")
    return members


def create_file(directory: int, filename: str, content: bytes):
    """Create a new private file relative to an already opened directory."""
    open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    file_fd = os.open(filename, open_flags, 0o600, dir_fd=directory)

    try:
        os.fchmod(file_fd, 0o600)

        with os.fdopen(file_fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()

        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def stage(payload: bytes, target: Path = TARGET):
    """Reject an already populated volume; never overwrite or reload inputs."""
    (config_bytes, dsn_bytes, abi_members) = decode(payload)

    if os.geteuid() != 0:
        raise InputError("Input staging must use the isolated provisioning identity.")

    if target.is_symlink() or not target.is_dir():
        raise InputError("Input destination is not an existing private mounted directory.")

    if any(target.iterdir()):
        raise InputError("Input volume is already populated; stage into a new volume.")

    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    try:
        create_file(directory_fd, CONFIG, config_bytes)
        create_file(directory_fd, DSN, dsn_bytes)
        _stage_abis(directory_fd, abi_members)

        # Files are written from the admitted envelope and fsynced. A
        # separate runtime-UID process verifies their readability after
        # ownership changes; no same-process readback is needed here.
        # CAP_CHOWN does not imply CAP_FOWNER. Set directory permissions while
        # still its owner; never widen them to compensate for UID mapping.
        os.chown(CONFIG, OWNER, OWNER, dir_fd=directory_fd, follow_symlinks=False)
        os.chown(DSN, OWNER, OWNER, dir_fd=directory_fd, follow_symlinks=False)
        os.fchmod(directory_fd, 0o700)
        os.fchown(directory_fd, OWNER, OWNER)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _stage_abis(directory_fd: int, members: dict[str, bytes]) -> None:
    os.mkdir("abi", mode=0o700, dir_fd=directory_fd)
    abi_directory = os.open("abi", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory_fd)
    try:
        for (name, content) in members.items():
            create_file(abi_directory, name, content)
            os.chown(name, OWNER, OWNER, dir_fd=abi_directory, follow_symlinks=False)
        os.fchmod(abi_directory, 0o700)
        os.fchown(abi_directory, OWNER, OWNER)
        os.fsync(abi_directory)
    finally:
        os.close(abi_directory)


def verify(target: Path = TARGET) -> None:
    """Admit the staged catalog as the actual unprivileged worker."""
    if (os.getuid(), os.getgid()) != (OWNER, OWNER):
        raise InputError("Runtime input check must use the selected worker identity.")

    if target.is_symlink():
        raise InputError("Runtime input directory must not be a symlink.")

    directory = target.stat()
    if (not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700
            or (directory.st_uid, directory.st_gid) != (OWNER, OWNER)):
        raise InputError("Runtime input directory has unexpected permissions.")

    abi_directory = target / "abi"
    abi_metadata = abi_directory.lstat()
    if (not stat.S_ISDIR(abi_metadata.st_mode) or stat.S_IMODE(abi_metadata.st_mode) != 0o700
            or (abi_metadata.st_uid, abi_metadata.st_gid) != (OWNER, OWNER)):
        raise InputError("Staged ABI directory has unexpected permissions.")

    members = tuple(islice(abi_directory.iterdir(), MAX_ABI_FILES + 1))
    if not 1 <= len(members) <= MAX_ABI_FILES:
        raise InputError("Staged ABI file count is outside its bound.")
    for path in (target / CONFIG, target / DSN, *members):
        metadata = path.lstat()

        if (not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_uid, metadata.st_gid) != (OWNER, OWNER)
            or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise InputError("Runtime input file has unexpected permissions.")

        with path.open("rb") as stream:
            if not stream.read(1):
                raise InputError("Runtime input file is empty.")

    # The catalog owns ABI admission and source identity. Do not relay its
    # messages from a private snapshot into operator diagnostics.
    from conix.abi import ResourceExceeded, UnsupportedABI
    from conix.catalog import ConfigurationError, prepare_catalog

    try:
        prepare_catalog(target / CONFIG)
    except (ConfigurationError, ResourceExceeded, UnsupportedABI) as error:
        raise InputError(f"Staged catalog was refused ({type(error).__name__}).") from error


def main(argv: list[str] | None = None) -> None:
    """Dispatch two explicit operations with different effective identities."""
    operation = sys.argv[1:] if argv is None else argv
    if operation not in (["stage"], ["verify"]):
        raise SystemExit("Use python -m conix.input_staging stage|verify.")

    try:
        if operation == ["stage"]:
            payload = sys.stdin.buffer.read(MAX_ENVELOPE + 1)
            stage(payload)
        else:
            verify()
    except InputError as error:
        # The messages are source-controlled and never include input bytes.
        phase = "staging" if operation == ["stage"] else "verification"
        raise SystemExit(f"Conix input {phase} failed (InputError): {error}") from None
    except OSError as error:
        raise SystemExit(
            f"Conix input staging failed ({type(error).__name__}): "
            "inspect volume ownership and the selected private input files."
        ) from None
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        raise SystemExit(
            f"Conix input {operation[0]} failed ({type(error).__name__}) "
            f"at {Path(frame.filename).name}:{frame.lineno}."
        ) from None


if __name__ == "__main__":
    main()
