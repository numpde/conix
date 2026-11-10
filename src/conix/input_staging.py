"""Provision fixed private inputs once, through an isolated stdin-only process.

The operator runs this module as UID 0 with CAP_CHOWN, no network and a
read-only root filesystem. It passes ownership to the restricted runtime UID
before the worker receives the volume read-only.
"""

import os
import stat
import struct
import sys
import tomllib
from pathlib import Path

MAGIC = b"CONIX-INPUTS-V1\n"
MAX_CONFIG = 64 * 1024
MAX_DSN = 8192
TARGET = Path("/run/conix")
CONFIG = "config.toml"
DSN = "connection.dsn"
OWNER = 65532


class InputError(ValueError):
    """A source-controlled diagnostic that cannot contain credential bytes."""


def decode(payload: bytes) -> tuple[bytes, bytes]:
    """Validate the length-prefixed envelope while preserving the original bytes."""
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + 8:
        raise InputError("Staged input envelope has an invalid version or length.")

    (config_size, dsn_size) = struct.unpack_from("!II", payload, len(MAGIC))

    if not 0 < config_size <= MAX_CONFIG or not 0 < dsn_size <= MAX_DSN:
        raise InputError("Staged configuration or credential input exceeds its bound.")

    content_offset = len(MAGIC) + 8

    if len(payload) != content_offset + config_size + dsn_size:
        raise InputError("Staged inputs were truncated or have trailing bytes.")

    config_bytes = payload[content_offset:content_offset + config_size]
    dsn_bytes = payload[content_offset + config_size:]

    try:
        config_document = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise InputError("Staged configuration is not valid UTF-8 TOML.") from error

    if (
        type(config_document) is not dict
        or type(config_document.get("database")) is not dict
        or config_document["database"].get("dsn_file") != str(TARGET / DSN)
    ):
        raise InputError("Staged config must select /run/conix/connection.dsn explicitly.")

    try:
        dsn_text = dsn_bytes.decode("utf-8").strip()
    except UnicodeError as error:
        raise InputError("Staged credential bytes must be UTF-8.") from error

    if not dsn_text or "\x00" in dsn_text:
        raise InputError("Staged credential file is empty or contains a NUL byte.")

    return config_bytes, dsn_bytes


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
    (config_bytes, dsn_bytes) = decode(payload)

    if os.geteuid() != 0:
        raise InputError("Input staging must use the isolated provisioning identity.")

    if target.is_symlink() or not target.is_dir():
        raise InputError("Input destination is not an existing private mounted directory.")

    if any(target.iterdir()):
        raise InputError("Input volume is already populated; stage into a new volume.")

    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    try:
        directory_mode = os.fstat(directory_fd).st_mode

        if not stat.S_ISDIR(directory_mode):
            raise InputError("Mounted input destination is not a directory.")

        create_file(directory_fd, CONFIG, config_bytes)
        create_file(directory_fd, DSN, dsn_bytes)

        # The private envelope is a transport proof, not a substitute for
        # the future catalog's complete ABI/source admission at preparation.
        # Check the exact file bytes while the provisioner still owns them.
        if (target / CONFIG).read_bytes() != config_bytes or (target / DSN).read_bytes() != dsn_bytes:
            raise InputError("Staged input files differ from the admitted private envelope.")

        # CAP_CHOWN does not imply CAP_FOWNER. Set directory permissions while
        # still its owner; never widen them to compensate for UID mapping.
        os.chown(CONFIG, OWNER, OWNER, dir_fd=directory_fd, follow_symlinks=False)
        os.chown(DSN, OWNER, OWNER, dir_fd=directory_fd, follow_symlinks=False)
        os.fchmod(directory_fd, 0o700)
        os.fchown(directory_fd, OWNER, OWNER)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main():
    # These inputs are private; subprocess stderr may only expose fixed errors.
    try:
        staged_payload = sys.stdin.buffer.read(len(MAGIC) + 8 + MAX_CONFIG + MAX_DSN + 1)
        stage(staged_payload)
    except InputError as error:
        raise SystemExit(f"Conix input staging failed (InputError): {error}") from None
    except (OSError, ValueError) as error:
        raise SystemExit(
            f"Conix input staging failed ({type(error).__name__}): inspect volume permissions and the private configuration."
        ) from None


if __name__ == "__main__":
    main()
