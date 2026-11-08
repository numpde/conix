"""One-shot, stdin-only provisioning of fixed inputs in an engine-owned volume.

This module is NOT part of the conix worker entry point. The operator invokes
it in the selected runtime image as uid 0 with only CAP_CHOWN, network none, a
read-only root filesystem, and a newly created private data volume. The worker
later runs as uid 65532 with no capabilities and the volume mounted read-only.
"""

import os
from pathlib import Path
import stat
import struct
import sys
import tomllib

MAGIC = b"CONIX-INPUTS-V1\n"
MAX_CONFIG = 64 * 1024
MAX_DSN = 8192
TARGET = Path("/run/conix")
CONFIG = "config.toml"
DSN = "connection.dsn"
OWNER = 65532


class InputError(ValueError):
    pass


def decode(payload: bytes) -> tuple[bytes, bytes]:
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + 8:
        raise InputError("Staged input envelope has an invalid version or length.")
    config_size, dsn_size = struct.unpack_from("!II", payload, len(MAGIC))
    if not 0 < config_size <= MAX_CONFIG or not 0 < dsn_size <= MAX_DSN:
        raise InputError("Staged configuration or credential input exceeds its bound.")
    start = len(MAGIC) + 8
    if len(payload) != start + config_size + dsn_size:
        raise InputError("Staged inputs were truncated or have trailing bytes.")
    config = payload[start:start + config_size]
    secret = payload[start + config_size:]
    try:
        table = tomllib.loads(config.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise InputError("Staged configuration is not valid UTF-8 TOML.") from error
    if (type(table) is not dict
            or type(table.get("database")) is not dict
            or table["database"].get("dsn_file") != str(TARGET / DSN)):
        raise InputError("Staged config must select /run/conix/connection.dsn explicitly.")
    try:
        decoded_dsn = secret.decode("utf-8").strip()
    except UnicodeError as error:
        raise InputError("Staged credential bytes must be UTF-8.") from error
    if not decoded_dsn or "\x00" in decoded_dsn:
        raise InputError("Staged credential file is empty or contains a NUL byte.")
    return config, secret


def create_file(directory: int, filename: str, content: bytes):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(filename, flags, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage(payload: bytes, target: Path = TARGET):
    """Reject reusing an initialized volume; no overwrite or hot reload."""
    config, secret = decode(payload)
    if os.geteuid() != 0:
        raise InputError("Input staging must use the isolated provisioning identity.")
    if target.is_symlink() or not target.is_dir():
        raise InputError("Input destination is not an existing private mounted directory.")
    if any(target.iterdir()):
        raise InputError("Input volume is already populated; stage into a new volume.")
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISDIR(mode):
            raise InputError("Mounted input destination is not a directory.")
        create_file(fd, CONFIG, config)
        create_file(fd, DSN, secret)
        # Validate while the staging identity still owns both private files.
        # A uid-0 process with only CAP_CHOWN cannot bypass DAC checks after
        # ownership changes to the runtime uid.
        from conix.configuration import load_settings, read_dsn
        parsed = load_settings(target / CONFIG)
        if parsed.database_dsn_file != target / DSN:
            raise InputError("Staged settings point outside the private credential file.")
        read_dsn(target / DSN)
        os.chown(CONFIG, OWNER, OWNER, dir_fd=fd, follow_symlinks=False)
        os.chown(DSN, OWNER, OWNER, dir_fd=fd, follow_symlinks=False)
        # Let only the runtime uid traverse the directory and read its files.
        # CAP_CHOWN does not imply CAP_FOWNER. Change mode while we still own
        # the directory, then transfer ownership to the runtime identity.
        os.fchmod(fd, 0o700)
        os.fchown(fd, OWNER, OWNER)
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    # One bounded read; never log the payload, the DSN, or raw exception bytes.
    try:
        payload = sys.stdin.buffer.read(len(MAGIC) + 8 + MAX_CONFIG + MAX_DSN + 1)
        stage(payload)
    except (InputError, OSError, ValueError) as error:
        raise SystemExit(f"Conix input staging refused the private volume or input schema: {type(error).__name__}.") from None


if __name__ == "__main__":
    main()
