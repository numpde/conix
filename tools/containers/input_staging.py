"""Host-owned private input snapshot staging for Conix containers.

Only this component reads the selected catalog/ABI and runtime DSN files,
stages them through the networkless stdin transport, and verifies or retires
the exact owned Docker volume. It does not own deployment lifecycle.
"""

import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import subprocess
import sys
import tomllib

from tools.containers.client import IMAGE_ID, OperationError

ROOT = Path(__file__).resolve().parents[2]


def _staging_failure(stderr):
    """Use only fixed producer fields; never relay arbitrary stderr."""
    from tools.containers.diagnostics import explanation

    return explanation(stderr, "unknown", phases={"staging", "verification"})


def _declared_abi_names(configuration):
    """Project transport paths without interpreting the selected catalog."""
    try:
        contracts = tomllib.loads(configuration.decode("utf-8")).get("contracts")
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise OperationError("Cannot read ABI paths from the selected TOML settings.") from error

    if not isinstance(contracts, dict) or not 1 <= len(contracts) <= 32:
        raise OperationError("Staged settings must select one to 32 contracts.")

    names = {}
    for contract in contracts.values():
        path = contract.get("abi_file") if isinstance(contract, dict) else None
        if not isinstance(path, str) or not (
            match := re.fullmatch(r"abi/([A-Za-z0-9][A-Za-z0-9_.-]{0,122}\.json)", path)
        ):
            raise OperationError("Staged ABI paths must have the form abi/<name>.json.")
        names[match[1]] = None
    return names


class InputStaging:
    """Reusable host-side staging behavior for the selected Docker client."""

    def private_bytes(self, value, max_size, *, secret, directory=None):
        """Read a selected operator-owned regular file without following its symlink."""
        source_path = Path(value)

        if directory is None and not source_path.is_absolute():
            raise OperationError("Staged input files must use absolute host paths.")

        open_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            source_fd = os.open(source_path, open_flags, dir_fd=directory)
            with os.fdopen(source_fd, "rb") as stream:
                file_stat = os.fstat(source_fd)
                if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid():
                    raise OperationError("Staged input must be an operator-owned regular file.")
                if secret and file_stat.st_mode & 0o077:
                    raise OperationError("Database credential source must have no group/other permissions.")
                if not secret and file_stat.st_mode & 0o022:
                    raise OperationError("Settings and ABI sources must not be group/other writable.")
                input_bytes = stream.read(max_size + 1)
        except OSError as error:
            subject = "database credential" if secret else "catalog input"
            raise OperationError(f"Cannot read selected {subject} (errno={error.errno}).") from error

        if not 0 < len(input_bytes) <= max_size:
            raise OperationError("Staged input file is empty or exceeds its size limit.")

        return input_bytes

    def catalog_snapshot(self, config_path):
        """Copy only the declared ABI members relative to the selected settings directory."""
        config_path = Path(config_path)
        if not config_path.is_absolute() or not config_path.name:
            raise OperationError("Staged input files must use absolute host paths.")

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            directory = os.open(config_path.parent, directory_flags)
        except OSError as error:
            raise OperationError(f"Cannot open selected settings directory (errno={error.errno}).") from error
        try:
            configuration = self.private_bytes(config_path.name, 64 * 1024, secret=False, directory=directory)
            names = _declared_abi_names(configuration)
            try:
                abi_directory = os.open("abi", directory_flags, dir_fd=directory)
            except OSError as error:
                raise OperationError(f"Cannot open adjacent ABI directory (errno={error.errno}).") from error
            try:
                members = {}
                remaining = 8 * 1024 * 1024
                for name in names:
                    member = self.private_bytes(name, min(remaining, 1024 * 1024),
                                                secret=False, directory=abi_directory)
                    members[name] = member
                    remaining -= len(member)
            finally:
                os.close(abi_directory)
        finally:
            os.close(directory)
        return (configuration, members)

    def stage_inputs(self, image, config_path, dsn_path, *, proof=False, announce=True):
        """Stage one private snapshot in a new volume; never mount host secrets."""
        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("INDEXER_IMAGE must select a prebuilt immutable indexer image.")

        self.require_role(image, "indexer")

        (config_bytes, abi_members) = self.catalog_snapshot(config_path)
        dsn_bytes = self.private_bytes(dsn_path, 8192, secret=True)

        # The contained verifier compiles the exact staged catalog. The DSN
        # travels only in private stdin to the networkless provisioner.
        staging_payload = (
            b"CONIX-INPUTS-V2\n"
            + struct.pack("!III", len(config_bytes), len(dsn_bytes), len(abi_members))
            + config_bytes + dsn_bytes
            + b"".join(
                struct.pack("!HI", len(name), len(member)) + name.encode("ascii") + member
                for (name, member) in abi_members.items()
            )
        )

        volume_token = secrets.token_hex(12)
        input_volume_name = "conix-inputs-" + volume_token

        try:
            created_volume_name = self.run(
                "volume", "create", "--label", "io.conix.input-token=" + volume_token,
                "--name", input_volume_name, timeout=15, capture=True,
            ).strip()
        except OperationError as error:
            raise OperationError(
                f"Cannot confirm creation of private volume {input_volume_name} on {self.endpoint}. {error}"
            ) from error

        if created_volume_name != input_volume_name:
            raise OperationError(
                f"Engine {self.endpoint} returned a different volume identity. "
                f"Inspect whether the requested {input_volume_name} exists."
            )

        cleanup_required = True
        primary_failure = None

        try:
            staging_argv = self.docker_command_prefix + [
                "run", "--rm", "--interactive", "--pull", "never", "--network", "none",
                "--read-only", "--user", "0:0", "--cap-drop", "ALL",
                "--cap-add", "CHOWN", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "32", "--memory", "128m",
                "--mount", f"type=volume,source={input_volume_name},target=/run/conix",
                "--entrypoint", "python", image, "-I", "-m", "conix.input_staging", "stage",
            ]

            try:
                staging_result = subprocess.run(
                    staging_argv, input=staging_payload, capture_output=True, cwd=ROOT,
                    env=self.docker_environment, timeout=40, check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise OperationError("Input staging timed out; the container outcome is unconfirmed.") from error

            if staging_result.returncode:
                from tools.containers.diagnostics import explanation

                reason = explanation(
                    staging_result.stderr, staging_result.returncode, phases={"staging"},
                )
                raise OperationError(
                    f"Conix input staging failed: "
                    f"{reason or f'exit={staging_result.returncode}; effect=unconfirmed; no admitted diagnostic'}."
                )

            self.verify_inputs(image, input_volume_name)

            if not proof:
                if announce:
                    print(input_volume_name, flush=True)
                cleanup_required = False
        except BaseException as error:
            primary_failure = error
            raise
        finally:
            if cleanup_required:
                try:
                    self.remove_inputs(input_volume_name, token=volume_token)
                except Exception as cleanup_error:
                    cleanup_message = (
                        f"Cannot confirm cleanup of owned private volume {input_volume_name}; "
                        f"inspect selected Engine {self.endpoint}."
                    )
                    if primary_failure is None:
                        raise OperationError(cleanup_message) from cleanup_error
                    try:
                        print(cleanup_message, file=sys.stderr)
                    except OSError:
                        pass  # Preserve the original operation failure.

        return None if proof else input_volume_name

    def verify_inputs(self, image, volume):
        """Verify staged inputs as the runtime UID and admit only fixed diagnostics."""
        argv = self.docker_command_prefix + [
            "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "128m",
            "--mount", f"type=volume,source={volume},target=/run/conix,readonly",
            "--entrypoint", "python", image,
            "-I", "-m", "conix.input_staging", "verify",
        ]
        try:
            result = subprocess.run(
                argv, cwd=ROOT, env=self.docker_environment,
                timeout=30, check=False, capture_output=True,
            )
        except subprocess.TimeoutExpired as error:
            raise OperationError(
                "Input verification timed out; the contained check result is unconfirmed."
            ) from error
        except OSError as error:
            raise OperationError(
                f"Input verification could not start ({type(error).__name__}, errno={error.errno})."
            ) from error

        if result.returncode:
            from tools.containers.diagnostics import explanation

            reason = explanation(result.stderr, result.returncode, phases={"verification"})
            raise OperationError(
                "Conix input verification failed: "
                + (reason or f"exit={result.returncode}; no admitted diagnostic."),
                exit_code=result.returncode,
            )

    def remove_inputs(self, volume, *, token=None):
        """Remove only a private volume with an exact matching ownership label."""
        if not re.fullmatch(r"conix-inputs-[a-f0-9]{24}", volume or ""):
            raise OperationError("Only conix-owned input-volume identities can be removed.")

        volume_records = json.loads(self.run("volume", "inspect", volume, timeout=10, capture=True))
        recorded_token = volume_records[0].get("Labels", {}).get("io.conix.input-token") if len(volume_records) == 1 else None
        expected_token = token or volume.removeprefix("conix-inputs-")

        if recorded_token != expected_token:
            raise OperationError("Private input-volume label does not match its identity; refusing removal.")

        self.run("volume", "rm", volume, timeout=30)

