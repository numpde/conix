"""One explicitly selected local Docker Engine for each container operation.

Host-side orchestration only. Runtime code never receives the Docker socket.
"""
import argparse
import json
import os
import re
import secrets
import stat
import struct
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from tools.containers.client import (
    CONTEXT_NAME,
    IMAGE_ID,
    DockerClient,
    OperationError,
    select_endpoint,
)
from tools.wheelhouse.policy import PoolError

ROOT = Path(__file__).resolve().parents[2]


def pinned_images():
    """Validate reviewed image pins without contacting Docker."""
    image_manifest = json.loads((ROOT / "containers/images.json").read_text(encoding="utf-8"))

    if set(image_manifest) != {"python_base", "postgres", "foundry", "dockerfile_frontend"}:
        raise OperationError("Reviewed manifest must contain python_base, postgres, foundry and dockerfile_frontend.")

    for (image_role, image_reference) in image_manifest.items():
        if type(image_reference) is not str or "@sha256:" not in image_reference or not IMAGE_ID.fullmatch(
            "sha256:" + image_reference.rsplit("@sha256:", 1)[-1]
        ):
            raise OperationError(f"Reviewed {image_role} base image must have a complete sha256 pin.")

    return image_manifest


def _staging_failure(stderr):
    """Admit only one fixed, bounded, nonsecret staging diagnostic."""
    diagnostic_prefix = "Conix input staging failed (InputError): "

    try:
        stderr_lines = stderr[-4096:].decode("utf-8").splitlines()
    except (UnicodeError, AttributeError):
        return None

    for diagnostic_line in reversed(stderr_lines):
        if re.fullmatch(
            r"Conix input stage failed \([A-Za-z][A-Za-z0-9_]{0,63}\) at [A-Za-z0-9_]{1,64}\.py:[1-9][0-9]{0,5}\.",
            diagnostic_line,
        ):
            return diagnostic_line
        if not diagnostic_line.startswith(diagnostic_prefix):
            continue

        safe_reason = diagnostic_line[len(diagnostic_prefix):]

        if (
            1 <= len(safe_reason) <= 240
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.,:/()-]*", safe_reason)
            and not any(marker in safe_reason.lower() for marker in ("password", "private", "secret", "://"))
            and re.search(r"(?:0x)?[0-9a-fA-F]{64}", safe_reason) is None
        ):
            return safe_reason

    return None


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


class Docker(DockerClient):
    """Conix image, input and test lifecycle on a selected Docker client."""

    def __init__(self, endpoint, config_dir):
        super().__init__(endpoint, config_dir, working_dir=ROOT)

    def acquire_image(self, role, images):
        """Pull only the selected, digest-pinned component image."""
        image_key_by_role = {
            "python-base": "python_base",
            "postgres": "postgres",
            "dockerfile-frontend": "dockerfile_frontend",
            "foundry": "foundry",
        }

        if role not in image_key_by_role:
            raise OperationError("Image acquisition requires an explicitly declared component role.")

        self.run(
            "pull", "--platform", "linux/amd64", images[image_key_by_role[role]],
            timeout=240 if role == "foundry" else 180,
        )

    def require_role(self, image, role):
        """Check the local image's immutable identity and declared runtime role."""
        if role not in ("indexer", "test-runner", "evm-test-runner"):
            raise OperationError("An image role must be one of the reviewed Conix roles.")

        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("An image role check requires an immutable sha256 image ID.")

        if self.require_image(image) != image:
            raise OperationError("The requested immutable image ID changed.")

        actual_image_role = self.run(
            "image", "inspect", "--format", '{{index .Config.Labels "io.conix.image-role"}}',
            image, capture=True, timeout=10,
        ).strip()

        if actual_image_role != role:
            raise OperationError(f"Selected image does not have the required {role} role.")

    def check_indexer(self, image):
        self.require_role(image, "indexer")
        self.run(
            "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "64", "--memory", "256m", "--user", "65532:65532",
            "--entrypoint", "python", image,
            "-I", "-m", "conix.installation_check",
            timeout=30,
            capture=True,
        )
        print("Verified installed package imports and ABI behavior.", file=sys.stderr)

    def check_evm_test_runner(self, image):
        """Verify offline test-runner Python and Cast without fixture secrets."""
        self.require_role(image, "evm-test-runner")

        confinement_args = (
            "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "64", "--memory", "256m",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
        )
        self.run(*confinement_args, "--entrypoint", "cast", image, "--version", timeout=20, capture=True)
        # The fixture image runs an actual unittest module. No embedded
        # interpreter program or wallet material appears in Docker argv.
        self.run(
            *confinement_args, "--entrypoint", "python", image,
            "-I", "-m", "unittest", "discover",
            "-s", "/tests", "-p", "evm_image_check.py", "-v",
            timeout=30,
            capture=True,
        )

        print("Verified offline EVM acceptance runner image and Cast signer.", file=sys.stderr)

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
                admitted_reason = _staging_failure(staging_result.stderr)
                safe_reason = admitted_reason or "no admitted nonsecret diagnostic returned"
                raise OperationError(f"Conix input staging failed (exit {staging_result.returncode}): {safe_reason}.")

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
        """Check staged ownership and permissions as the actual runtime UID."""
        self.run(
            "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "128m",
            "--mount", f"type=volume,source={volume},target=/run/conix,readonly",
            "--entrypoint", "python", image,
            "-I", "-m", "conix.input_staging", "verify",
            timeout=30,
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

    def deployment(self, *, operation, name, image, volume, database, rpc):
        """Separate recovery by project identity from prospective launch admission."""
        if type(name) is not str or not CONTEXT_NAME.fullmatch(name):
            raise OperationError("Select one valid Conix deployment name.")

        project = "conix-" + name
        service_labels = (
            "--filter", "label=com.docker.compose.project=" + project,
            "--filter", "label=com.docker.compose.service=indexer",
        )

        # These commands refer to existing Docker resources by labels.
        # They remain usable after the image, input volume or Compose
        # configuration changes or disappears.
        if operation == "status":
            self.run("ps", "--all", *service_labels, timeout=20)
            return

        if operation == "remove":
            observed_ids = self.run(
                "ps", "--all", *service_labels, "--format", "{{.ID}}",
                capture=True, timeout=20,
            ).splitlines()

            for container_id in observed_ids:
                if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                    raise OperationError("Docker returned an invalid Conix container identity.")

            for container_id in observed_ids:
                self.run("container", "stop", "--time", "90", container_id, timeout=105)
                self.run("container", "rm", container_id, timeout=30)

            print(f"Retired {len(observed_ids)} Conix indexer container(s) from {project}; "
                  "external networks, database and input volumes were retained.")
            return

        if operation != "config/check":
            raise OperationError("Generic indexer deployment is not installed.")

        for (label, selected) in (("database network", database), ("RPC network", rpc)):
            if type(selected) is not str or not CONTEXT_NAME.fullmatch(selected):
                raise OperationError(f"Select one valid {label} name.")
        if database == rpc:
            raise OperationError("Source RPC and database networks must be distinct.")
        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("Select one immutable INDEXER_IMAGE=sha256:<id>.")
        if not re.fullmatch(r"conix-inputs-[0-9a-f]{24}", volume or ""):
            raise OperationError("Select an explicit Conix input-volume identity.")

        from tools.containers.history import COMMAND as HISTORY_ADVANCE_COMMAND

        compose_argv = [
            "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
            "-p", project, "-f", str(ROOT / "compose/runtime.yml"),
        ]
        compose_environment = self.docker_environment.copy()
        compose_environment.update(
            CONIX_INDEXER_IMAGE=image,
            CONIX_INPUT_VOLUME=volume,
            CONIX_DATABASE_NETWORK=database,
            CONIX_RPC_NETWORK=rpc,
        )

        try:
            rendered_configuration = json.loads(self.run(
                *compose_argv, "config", "--format", "json",
                env=compose_environment, timeout=20, capture=True,
            ))
            rendered_services = rendered_configuration["services"]
            indexer_service = rendered_services["indexer"]
            input_mounts = indexer_service.get("volumes", [])
            rendered_networks = rendered_configuration["networks"]

            if (
                set(rendered_services) != {"indexer"}
                or indexer_service.get("image") != image
                or indexer_service.get("pull_policy") != "never"
                or indexer_service.get("entrypoint") != ["python"]
                or indexer_service.get("command") != HISTORY_ADVANCE_COMMAND
                or indexer_service.get("user") != "65532:65532"
                or indexer_service.get("read_only") is not True
                or indexer_service.get("privileged", False)
                or indexer_service.get("ports") or indexer_service.get("network_mode")
                or set(indexer_service.get("cap_drop", [])) != {"ALL"}
                or indexer_service.get("cap_add")
                or set(indexer_service.get("security_opt", [])) != {"no-new-privileges:true"}
                or set(indexer_service.get("networks", [])) != {"database", "rpc"}
                or indexer_service.get("devices") or indexer_service.get("device_cgroup_rules")
                or indexer_service.get("pid") or indexer_service.get("ipc") or indexer_service.get("uts")
                or len(input_mounts) != 1
                or input_mounts[0].get("type") != "volume"
                or input_mounts[0].get("source") not in ("inputs", volume)
                or input_mounts[0].get("target") != "/run/conix"
                or input_mounts[0].get("read_only") is not True
                or len(rendered_networks) != 2
                or rendered_networks["database"]["name"] != database
                or rendered_networks["rpc"]["name"] != rpc
            ):
                raise OperationError("Rendered runtime deployment violates image, input or confinement policy.")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise OperationError("Cannot validate the selected runtime deployment configuration.") from error

        print(f"Verified constrained prospective runtime Compose graph for {project}.")

    def test(self, image, images):
        """Run an isolated PostgreSQL test project and retire only its resources."""
        if not IMAGE_ID.fullmatch(image):
            raise OperationError("test/postgres/run requires immutable RUNNER_IMAGE=sha256:<id>.")

        self.require_role(image, "test-runner")
        self.require_image(images["postgres"])
        self.require_image(images["foundry"])

        test_project = "conix-tier0-" + secrets.token_hex(6)
        compose_argv = [
            "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
            "-p", test_project, "-f", str(ROOT / "compose/tests.yml"),
        ]
        compose_environment = self.docker_environment.copy()
        compose_environment["CONIX_TEST_RUNNER_IMAGE"] = image
        compose_environment["CONIX_POSTGRES_IMAGE"] = images["postgres"]
        compose_environment["CONIX_FOUNDRY_IMAGE"] = images["foundry"]

        try:
            rendered_configuration = json.loads(self.run(
                *compose_argv, "config", "--format", "json",
                env=compose_environment, timeout=20, capture=True,
            ))
            rendered_services = rendered_configuration["services"]
            rendered_networks = rendered_configuration["networks"]

            if (
                set(rendered_services) != {"tests", "postgres", "bootstrap", "initializer", "anvil"}
                or rendered_services["anvil"].get("image") != images["foundry"]
                or rendered_services["anvil"].get("user") != "65532:65532"
                or rendered_services["anvil"].get("read_only") is not True
                or rendered_services["anvil"].get("ports")
                or rendered_services["anvil"].get("networks") not in (["tests"], {"tests": None})
                or rendered_services["initializer"].get("image") != image
                or rendered_services["initializer"].get("read_only") is not True
                or rendered_services["initializer"].get("user") != "65532:65532"
                or "CONIX_TEST_OWNER_DSN" not in rendered_services["initializer"].get("environment", {})
                or "CONIX_TEST_ANVIL_OWNER_DSN" not in rendered_services["initializer"].get("environment", {})
                or any(
                    name in rendered_services["tests"].get("environment", {})
                    for name in ("CONIX_TEST_OWNER_DSN", "CONIX_TEST_ANVIL_OWNER_DSN")
                )
                or rendered_services["tests"].get("image") != image
                or rendered_services["tests"].get("read_only") is not True
                or rendered_services["tests"].get("user") != "65532:65532"
                or rendered_services["postgres"].get("image") != images["postgres"]
                or rendered_services["postgres"].get("user") != "999:999"
                or rendered_services["postgres"].get("read_only") is not True
                or rendered_services["bootstrap"].get("image") != images["postgres"]
                or rendered_services["bootstrap"].get("user") != "999:999"
                or rendered_services["bootstrap"].get("read_only") is not True
                or any(service.get("build") or service.get("ports") or service.get("privileged") or service.get("network_mode")
                       for service in rendered_services.values())
                or any(any(mount.get("type") == "bind" for mount in service.get("volumes", []))
                       for service in rendered_services.values())
                or len(rendered_networks) != 1
                or not next(iter(rendered_networks.values())).get("internal")
            ):
                raise OperationError("Rendered test services violate selected images, no-bind, network or confinement rules.")
        except (KeyError, ValueError) as error:
            raise OperationError("Cannot validate rendered test deployment.") from error

        test_failure = None

        try:
            # Bootstrap terminates successfully before tests; abort-on-exit
            # would stop PostgreSQL when that one-shot initialization finishes.
            self.run(
                *compose_argv, "up", "--detach", "--no-build", "--pull", "never",
                "--wait", "--wait-timeout", "60", "postgres", "anvil",
                env=compose_environment, timeout=90,
            )
            self.run(*compose_argv, "run", "--rm", "--no-deps", "--pull",
                     "never", "bootstrap", env=compose_environment, timeout=60)
            self.run(
                *compose_argv, "run", "--rm", "--no-deps", "--pull",
                "never", "initializer",
                env=compose_environment,
                timeout=90,
            )
            self.run(
                *compose_argv, "run", "--rm", "--no-deps", "--pull",
                "never", "tests",
                env=compose_environment,
                timeout=240,
            )
        except Exception as error:
            test_failure = error

            # Collect fixture-only diagnostics before disposable containers
            # disappear. Production private-volume logs are never captured here.
            try:
                self.run(*compose_argv, "ps", "--all", env=compose_environment, timeout=15)
                self.run(
                    *compose_argv, "logs", "--no-color", "--tail", "35",
                    "postgres", "anvil", "bootstrap", "initializer", "tests",
                    env=compose_environment, timeout=15,
                )
            except OperationError:
                pass
        finally:
            try:
                self.run(
                    *compose_argv, "down", "--volumes", "--remove-orphans",
                    "--timeout", "30", env=compose_environment, timeout=90,
                )
            except Exception as cleanup_error:
                recovery = (
                    f"Cleanup unconfirmed: project={test_project}, endpoint={self.endpoint}, "
                    f"compose={ROOT / 'compose/tests.yml'}. Inspect before removing."
                )
                print(recovery, file=sys.stderr)

                if test_failure is None:
                    raise OperationError(
                        f"Test project cleanup failed ({type(cleanup_error).__name__}). {recovery}"
                    ) from cleanup_error

                # The original test failure remains primary. Resource
                # retirement is a separate, unconfirmed outcome.
                test_failure.add_note(recovery)

        if test_failure is not None:
            raise test_failure


def main():
    """Dispatch the selected operation without interpreting Make values as code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-make", action="store_true")
    parser.add_argument("--context", default="")
    parser.add_argument("--origin", default="undefined")
    parser.add_argument("action", choices=(
        "image/python-base/acquire", "image/postgres/acquire",
        "image/dockerfile-frontend/acquire", "image/foundry/acquire",
        "wheelhouse/check", "wheelhouse/acquire",
        "image/indexer/build", "image/test-runner/build", "image/evm-test-runner/build",
        "image/indexer/check", "image/evm-test-runner/check",
        "test/postgres/run", "test/input-staging/run",
        "history/advance",
        "deployment/inputs/create", "deployment/inputs/remove",
        "deployment/config/check", "deployment/status", "deployment/remove",
    ))
    parser.add_argument("--wheelhouse")
    parser.add_argument("--image")
    parser.add_argument("--runner-image")
    parser.add_argument("--indexer-image")
    parser.add_argument("--config-file")
    parser.add_argument("--dsn-file")
    parser.add_argument("--volume")
    parser.add_argument("--deployment")
    parser.add_argument("--database-network")
    parser.add_argument("--rpc-network")
    options = parser.parse_args()

    try:
        if options.from_make:
            # Make forwards literal variables via $(value VAR); the operator
            # alone constructs argv, never a shell recipe using their contents.
            make_inputs = {name: os.environ.get("CONIX_MAKE_" + name, "") for name in (
                "DOCKER_CONTEXT", "WHEELHOUSE", "IMAGE", "RUNNER_IMAGE",
                "INDEXER_IMAGE", "CONFIG_FILE", "DSN_FILE",
                "INPUT_VOLUME", "DEPLOYMENT",
                "DATABASE_NETWORK", "RPC_NETWORK",
            )}

            options.context = make_inputs["DOCKER_CONTEXT"]
            options.wheelhouse = make_inputs["WHEELHOUSE"]
            options.image = make_inputs["IMAGE"]
            options.runner_image = make_inputs["RUNNER_IMAGE"]
            options.indexer_image = make_inputs["INDEXER_IMAGE"]
            options.config_file = make_inputs["CONFIG_FILE"]
            options.dsn_file = make_inputs["DSN_FILE"]
            options.volume = make_inputs["INPUT_VOLUME"]
            options.deployment = make_inputs["DEPLOYMENT"]
            options.database_network = make_inputs["DATABASE_NETWORK"]
            options.rpc_network = make_inputs["RPC_NETWORK"]

        docker_endpoint = select_endpoint(options.context, options.origin, working_dir=ROOT)

        with tempfile.TemporaryDirectory(prefix="conix-docker-client-") as directory:
            client_config_directory = Path(directory)
            (client_config_directory / "config.json").write_text('{"auths":{},"proxies":{}}\n', encoding="utf-8")

            docker = Docker(docker_endpoint, client_config_directory)
            image_manifest = pinned_images()

            if options.action.startswith("image/") and options.action.endswith("/acquire"):
                image_role = options.action.split("/")[1]
                docker.acquire_image(image_role, image_manifest)

            elif options.action in ("wheelhouse/check", "wheelhouse/acquire"):
                from tools.wheelhouse.operations import SharedWheelhouse

                wheel_pool = SharedWheelhouse(docker, image_manifest, options.wheelhouse)
                selected_wheels = wheel_pool.check() if options.action.endswith("/check") else wheel_pool.acquire()
                print(f"Verified {len(selected_wheels)} hash-locked artifacts in WHEELHOUSE.", file=sys.stderr)

            elif options.action == "test/postgres/run":
                docker.test(options.runner_image or "", image_manifest)

            elif options.action == "image/indexer/check":
                docker.check_indexer(options.image)

            elif options.action == "image/evm-test-runner/check":
                docker.check_evm_test_runner(options.image)

            elif options.action == "history/advance":
                from tools.containers.history import advance_history

                advance_history(
                    docker, image=options.indexer_image, volume=options.volume,
                    database=options.database_network, rpc=options.rpc_network,
                )

            elif options.action in ("deployment/inputs/create", "test/input-staging/run"):
                docker.stage_inputs(
                    image=options.indexer_image,
                    config_path=options.config_file,
                    dsn_path=options.dsn_file,
                    proof=(options.action == "test/input-staging/run"),
                )

            elif options.action == "deployment/inputs/remove":
                docker.remove_inputs(options.volume)

            elif options.action.startswith("deployment/"):
                docker.deployment(
                    operation=options.action.removeprefix("deployment/"),
                    name=options.deployment,
                    image=options.indexer_image,
                    volume=options.volume,
                    database=options.database_network,
                    rpc=options.rpc_network,
                )
            else:
                from tools.wheelhouse.operations import SharedWheelhouse

                image_role = options.action.split("/")[1]
                wheel_pool = SharedWheelhouse(docker, image_manifest, options.wheelhouse)
                print(wheel_pool.build(image_role))
    except (OperationError, PoolError, OSError) as error:
        raise SystemExit(f"Conix container operation failed ({type(error).__name__}): {error}") from None


if __name__ == "__main__":
    main()
