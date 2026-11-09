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
import tomllib
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from tools.wheelhouse.policy import PoolError

ROOT = Path(__file__).resolve().parents[2]
CONTEXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")

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


class OperationError(Exception):
    """Operator-safe failure with an optional subprocess exit code."""

    def __init__(self, detail, *, exit_code=None):
        super().__init__(detail)
        self.exit_code = exit_code


def local_socket(endpoint):
    """Allow local Unix sockets, never remote Docker endpoints."""
    try:
        parsed_endpoint = urlsplit(endpoint)

        return (
            parsed_endpoint.scheme == "unix"
            and not parsed_endpoint.netloc
            and not parsed_endpoint.query
            and not parsed_endpoint.fragment
            and parsed_endpoint.path.startswith("/")
            and parsed_endpoint.path != "/"
            and len(endpoint) <= 4096
            and not any(ord(character) < 32 for character in endpoint)
        )
    except ValueError:
        return False


def environment(client_config=None):
    """Drop ambient Docker, Compose and proxy authority from child processes."""
    excluded_prefixes = ("DOCKER_", "COMPOSE_", "BUILDX_", "BUILDKIT_")
    excluded_names = (
        "CONTAINER_HOST", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    )
    child_environment = {
        name: value for (name, value) in os.environ.items()
        if not name.startswith(excluded_prefixes) and name not in excluded_names
    }
    child_environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    child_environment["DOCKER_BUILDKIT"] = "1"

    if client_config is not None:
        child_environment["DOCKER_CONFIG"] = str(client_config)

    return child_environment


def _staging_failure(stderr):
    """Admit only one fixed, bounded, nonsecret staging diagnostic."""
    diagnostic_prefix = "Conix input staging failed (InputError): "

    try:
        stderr_lines = stderr[-4096:].decode("utf-8").splitlines()
    except (UnicodeError, AttributeError):
        return None

    for diagnostic_line in reversed(stderr_lines):
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


def _docker_operation(argv):
    """Report a known Docker verb without echoing its private arguments."""
    if "compose" in argv:
        compose_args = argv[argv.index("compose") + 1:]
        compose_action = next((value for value in compose_args if value in (
            "config", "up", "down", "run", "ps", "stop", "logs",
        )), "operation")

        return f"Docker Compose {compose_action}"

    for resource_kind in ("image", "volume", "network", "context", "container"):
        if resource_kind in argv:
            resource_index = argv.index(resource_kind)
            resource_action = (
                argv[resource_index + 1]
                if resource_index + 1 < len(argv) and argv[resource_index + 1] in (
                    "inspect", "create", "rm", "ls", "tag",
                ) else "operation"
            )

            return f"Docker {resource_kind} {resource_action}"

    for docker_action in ("run", "pull", "build", "info"):
        if docker_action in argv:
            return f"Docker {docker_action}"

    return "Docker operation"


def command(argv, *, env, timeout=60, capture=False, progress_to_stderr=False, public_diagnostic=False):
    """Run a bounded Docker command; only explicitly admitted diagnostics are public."""
    captures_output = capture or public_diagnostic

    try:
        completed_process = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            timeout=timeout,
            check=False,
            text=captures_output,
            capture_output=captures_output,
            stdout=None if captures_output else (sys.stderr if progress_to_stderr else None),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OperationError(f"{_docker_operation(argv)} failed to run ({type(error).__name__}).") from error

    if completed_process.returncode:
        # Only the network-enabled wheel worker may contribute bounded stderr;
        # generic Docker/Compose output can contain private fixture data.
        if public_diagnostic:
            safe_diagnostic = (completed_process.stderr or "")[-1200:].replace("\r", " ").replace("\x00", " ").strip()

            raise OperationError(
                f"Contained public wheel acquisition failed (exit {completed_process.returncode}): "
                + (safe_diagnostic or "no worker diagnostic returned."),
                exit_code=completed_process.returncode,
            )

        raise OperationError(
            f"{_docker_operation(argv)} failed (exit {completed_process.returncode}).",
            exit_code=completed_process.returncode,
        )

    if capture and len(completed_process.stdout) > 65536:
        raise OperationError("Docker returned an oversized response.")

    return completed_process.stdout if capture else None


def select_endpoint(name, origin):
    """Select an explicit local Engine or prompt the operator at a terminal."""
    if os.geteuid() == 0:
        raise OperationError(
            "Conix container operations refuse host root (UID 0). Use the selected "
            "Docker Engine's unprivileged operator account, not sudo."
        )

    context_listing = command(
        ["docker", "context", "ls", "--format", "{{.Name}}\t{{.DockerEndpoint}}"],
        env=environment(),
        timeout=10,
        capture=True,
    )

    context_endpoints = {}
    for line in context_listing.splitlines():
        (context_name, separator, context_endpoint) = line.partition("\t")

        if not separator or not CONTEXT_NAME.fullmatch(context_name) or context_name in context_endpoints:
            raise OperationError("Docker returned malformed or duplicate context entries.")

        context_endpoints[context_name] = context_endpoint

    local_endpoints = {
        context_name: context_endpoint for (context_name, context_endpoint) in context_endpoints.items()
        if local_socket(context_endpoint)
    }

    if origin == "command line":
        if not CONTEXT_NAME.fullmatch(name):
            raise OperationError("Select one named local Docker context on the Make command line.")

        if name not in local_endpoints:
            raise OperationError(f"Context {name!r} is missing or does not use a local Unix socket.")

        return local_endpoints[name]

    if not local_endpoints:
        raise OperationError("Docker does not list a usable local Unix-socket context.")

    try:
        with open("/dev/tty", "r") as reader, open("/dev/tty", "w") as writer:
            context_names = sorted(local_endpoints)
            writer.write("Choose a local Docker context (none selected automatically):\n")

            for (number, context_name) in enumerate(context_names, 1):
                writer.write(f"  {number}) {context_name}\n")

            writer.write("Context number: ")
            writer.flush()
            selected_number = reader.readline().strip()
    except OSError as error:
        raise OperationError(
            "No terminal. Pass DOCKER_CONTEXT=<name> on the Make command line. "
            f"Local contexts: {', '.join(sorted(local_endpoints))}."
        ) from error

    if not selected_number.isascii() or not selected_number.isdecimal() or str(int(selected_number)) != selected_number or not 1 <= int(selected_number) <= len(context_names):
        raise OperationError("Invalid context choice. No Docker Engine was selected.")

    return local_endpoints[context_names[int(selected_number) - 1]]


class Docker:
    """Pin one admitted local Engine for every operation and cleanup."""

    def __init__(self, endpoint, config_dir):
        self.endpoint = endpoint
        self.config_dir = config_dir
        self.docker_command_prefix = ["docker", "--config", str(config_dir), "--host", endpoint]
        self.docker_environment = environment(config_dir)

    def run(self, *args, timeout=60, capture=False, env=None, progress_to_stderr=False, public_diagnostic=False):
        return command(
            self.docker_command_prefix + list(args),
            env=self.docker_environment if env is None else env,
            timeout=timeout,
            capture=capture,
            progress_to_stderr=progress_to_stderr,
            public_diagnostic=public_diagnostic,
        )

    def require_image(self, reference):
        """Check the exact local image ID without publishing the image metadata."""
        actual_image_id = self.run(
            "image", "inspect", "--format", "{{.Id}}", reference,
            timeout=10, capture=True,
        ).strip()

        if not IMAGE_ID.fullmatch(actual_image_id):
            raise OperationError("Selected image is not a valid local image ID.")

        return actual_image_id

    def require_rootless_engine(self):
        """The local socket alone does not establish rootless daemon identity."""
        try:
            security_options = json.loads(self.run(
                "info", "--format", "{{json .SecurityOptions}}",
                timeout=10, capture=True,
            ))
        except (ValueError, TypeError) as error:
            raise OperationError("Cannot establish selected Docker Engine rootless status from daemon security options.") from error

        if type(security_options) is not list or not all(type(option) is str for option in security_options):
            raise OperationError("Selected Docker Engine returned malformed security options.")

        if "name=rootless" not in security_options:
            raise OperationError(
                "Runtime start requires a verified rootless Docker Engine; "
                "a local Unix socket alone does not qualify a rootful daemon."
            )

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
        self.run("run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                 "--pids-limit", "64", "--memory", "256m",
                 "--user", "65532:65532", image, "--help", timeout=30, capture=True)
        print("Verified offline indexer image and installed command.", file=sys.stderr)

    def check_evm_test_runner(self, image):
        """Verify offline test-runner Python and Cast without fixture secrets."""
        self.require_role(image, "evm-test-runner")

        confinement_args = (
            "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "64", "--memory", "256m",
        )
        self.run(*confinement_args, "--entrypoint", "cast", image, "--version", timeout=20, capture=True)
        self.run(
            *confinement_args, "--entrypoint", "python", image,
            "-I", "/tests/pay_to_play_integration.py", "--help", timeout=20, capture=True,
        )

        # Exercise the exact digest-signing command without contacting a live
        # RPC source or sending a private fixture key into logs.
        signing_check_script = (
            "import subprocess;"
            "test_digest='0x'+'ab'*32;"
            "test_key='0x'+'00'*31+'01';"
            "signing_result=subprocess.run(['cast','wallet','sign','--no-hash',test_digest,"
            "'--private-key',test_key],capture_output=True,text=True,timeout=10);"
            "signature=signing_result.stdout.strip();"
            "assert signing_result.returncode==0 and len(signature)==132 and signature[:2]=='0x';"
            "assert bytes.fromhex(signature[2:])[-1] in (27,28)"
        )
        self.run(*confinement_args, "--entrypoint", "python", image,
                 "-I", "-c", signing_check_script, timeout=20)

        print("Verified offline EVM test-runner image, Cast signing and scenario entry point.", file=sys.stderr)

    def private_bytes(self, value, max_size, *, secret):
        """Read an operator-owned regular file without following its final symlink."""
        source_path = Path(value)

        if not source_path.is_absolute():
            raise OperationError("Staged input files must use absolute host paths.")

        open_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        source_fd = os.open(source_path, open_flags)

        try:
            file_stat = os.fstat(source_fd)

            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid():
                raise OperationError("Staged input must be an operator-owned regular file.")

            if secret and file_stat.st_mode & 0o077:
                raise OperationError("Database credential source must have no group/other permissions.")

            if not secret and file_stat.st_mode & 0o022:
                raise OperationError("Conix settings source must not be group/other writable.")

            input_bytes = os.read(source_fd, max_size + 1)
        finally:
            os.close(source_fd)

        if not 0 < len(input_bytes) <= max_size:
            raise OperationError("Staged input file is empty or exceeds its size limit.")

        return input_bytes

    def stage_inputs(self, image, config_path, dsn_path, *, proof=False, announce=True):
        """Create and own a unique input volume, with no host bind mounts."""
        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("INDEXER_IMAGE must select a prebuilt immutable indexer image.")

        self.require_role(image, "indexer")

        # No operator input becomes a bind mount into the runtime. Stage a
        # one-time snapshot and then prove access as the actual runtime UID.
        config = self.private_bytes(config_path, 64 * 1024, secret=False)
        dsn = self.private_bytes(dsn_path, 8192, secret=True)
        try:
            settings_document = tomllib.loads(config.decode("utf-8"))
            if settings_document["database"]["dsn_file"] != "/run/conix/connection.dsn":
                raise ValueError
        except (UnicodeError, KeyError, TypeError, ValueError) as error:
            raise OperationError("Stage settings with database.dsn_file=/run/conix/connection.dsn; no host path is mounted.") from error

        envelope = b"CONIX-INPUTS-V1\n" + struct.pack("!II", len(config), len(dsn)) + config + dsn
        token = secrets.token_hex(12)
        volume = "conix-inputs-" + token
        created = self.run(
            "volume", "create", "--label", "io.conix.input-token=" + token,
            "--name", volume, timeout=15, capture=True,
        ).strip()
        if created != volume:
            raise OperationError("Created input volume identity differs from the selected name; inspect the Engine.")

        remove = proof
        try:
            # Root only in this short-lived, networkless provisioner. It receives
            # exactly CAP_CHOWN to hand 0600 files and 0700 directory to uid 65532.
            # No secret appears in argv, environment, Docker labels, or image layers.
            argv = self.docker_command_prefix + [
                "run", "--rm", "--interactive", "--pull", "never", "--network", "none",
                "--read-only", "--user", "0:0", "--cap-drop", "ALL",
                "--cap-add", "CHOWN", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "32", "--memory", "128m",
                "--mount", f"type=volume,source={volume},target=/run/conix",
                "--entrypoint", "python", image, "-m", "conix.input_staging",
            ]
            staging_result = subprocess.run(
                argv, input=envelope, capture_output=True, cwd=ROOT,
                env=self.docker_environment, timeout=40, check=False,
            )
            if staging_result.returncode:
                cause = _staging_failure(staging_result.stderr)
                detail = cause or "no admitted nonsecret diagnostic returned"
                raise OperationError(f"Conix input staging failed (exit {staging_result.returncode}): {detail}.")

            self.verify_inputs(image, volume)

            if not proof:
                remove = False
                if announce:
                    print(volume)
        except Exception:
            remove = True
            raise
        finally:
            if remove:
                try:
                    self.remove_inputs(volume, token=token)
                except Exception as cleanup_error:
                    active = sys.exc_info()[1]
                    cause = (
                        f" Original staging failure: {active}."
                        if isinstance(active, OperationError) else
                        f" Original staging failure: {type(active).__name__}."
                        if active is not None else ""
                    )
                    raise OperationError(
                        f"Cannot confirm cleanup of owned private volume {volume}; "
                        f"inspect selected Engine {self.docker_command_prefix[-1]}.{cause}"
                    ) from cleanup_error

        return None if proof else volume

    def verify_inputs(self, image, volume):
        """Prove access as the *actual* runtime uid without mapping host paths."""
        check = (
            "import os,stat;"
            "from pathlib import Path;"
            "from conix.configuration import load_settings,read_dsn;"
            "d=Path('/run/conix');"
            "assert os.getuid()==65532;"
            "assert stat.S_IMODE(d.stat().st_mode)==0o700;"
            "assert stat.S_IMODE((d/'connection.dsn').stat().st_mode)==0o600;"
            "assert (d/'connection.dsn').stat().st_uid==65532;"
            "assert load_settings(d/'config.toml').database_dsn_file==d/'connection.dsn';"
            "assert read_dsn(d/'connection.dsn')"
        )
        self.run(
            "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "128m",
            "--mount", f"type=volume,source={volume},target=/run/conix,readonly",
            "--entrypoint", "python", image, "-c", check,
            timeout=30,
        )

    def remove_inputs(self, volume, *, token=None):
        """Remove only the exact volume whose ownership label still matches."""
        if not re.fullmatch(r"conix-inputs-[a-f0-9]{24}", volume or ""):
            raise OperationError("Only conix-owned input-volume identities can be removed.")

        values = json.loads(self.run("volume", "inspect", volume, timeout=10, capture=True))
        if len(values) != 1 or values[0].get("Labels", {}).get("io.conix.input-token") != (token or volume.removeprefix("conix-inputs-")):
            raise OperationError("Private input-volume label does not match its identity; refusing removal.")

        self.run("volume", "rm", volume, timeout=30)

    def initialize_schema(self, image, config_path, owner_dsn_path, database):
        """Confined, one-shot owner-only schema operation, no worker credentials.

        Reuse the existing private input stager, but place owner credentials in
        an ephemeral volume mounted only into the installation container.
        The normal worker volume is never mounted here or modified.
        """
        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("Owner initialization requires immutable INDEXER_IMAGE=sha256:<id>.")

        if type(database) is not str or not CONTEXT_NAME.fullmatch(database):
            raise OperationError("Choose one existing database network for schema initialization.")

        self.require_rootless_engine()
        self.require_role(image, "indexer")
        actual = self.run(
            "network", "inspect", "--format", "{{.Name}}", database,
            timeout=10, capture=True,
        ).strip()
        if actual != database:
            raise OperationError("Selected owner-init database network is missing or has another identity.")

        volume = self.stage_inputs(image, config_path, owner_dsn_path, announce=False)
        failure = None
        try:
            self.run(
                "run", "--rm", "--init", "--pull", "never",
                "--network", database,
                "--read-only", "--user", "65532:65532",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                "--pids-limit", "64", "--memory", "256m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,uid=65532,gid=65532,mode=0700,size=16m",
                "--mount", f"type=volume,source={volume},target=/run/conix-owner,readonly",
                image, "init", "--config", "/run/conix-owner/config.toml",
                "--owner-dsn-file", "/run/conix-owner/connection.dsn",
                timeout=90,
            )
        except Exception as error:
            failure = error

        cleanup_failure = None
        try:
            self.remove_inputs(volume)
        except Exception as error:
            cleanup_failure = error

        if cleanup_failure is not None:
            first = (
                f" Original schema init failed: {failure}."
                if isinstance(failure, OperationError) else
                f" Original schema init failed ({type(failure).__name__})."
                if failure is not None else ""
            )
            raise OperationError(
                f"Schema init input-volume cleanup is unconfirmed for {volume} "
                f"on selected Engine {self.docker_command_prefix[-1]}.{first} Inspect exact volume ownership."
            ) from cleanup_failure

        if failure is not None:
            detail = str(failure) if isinstance(failure, OperationError) else type(failure).__name__
            raise OperationError(
                f"Conix schema init failed: {detail}. Check durable source identity "
                "before retrying the explicit idempotent owner operation."
            ) from failure

        print("Conix schema installed/verified through a separate owner-only session.")

    def deployment(self, *, operation, name, image, volume, database, rpc):
        """Render/admit one instance, then use that exact project on this Engine."""
        for (label, value) in (
            ("deployment", name), ("database network", database), ("RPC network", rpc),
        ):
            if type(value) is not str or not CONTEXT_NAME.fullmatch(value):
                raise OperationError(f"Select one valid {label} name without paths or control characters.")

        if database == rpc:
            raise OperationError("Source RPC and database networks must be separately selected.")

        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("Deployment requires immutable INDEXER_IMAGE=sha256:<id>.")

        # Resource existence is a start precondition, not a stop/status
        # precondition. Teardown must work after image, volume, network or
        # source files have disappeared from this Engine.
        if operation == "apply":
            self.require_rootless_engine()
            self.require_role(image, "indexer")

        # An input volume's identity and ownership label must agree before a
        # command can reuse it. The label is not a credential or permission.
        if not re.fullmatch(r"conix-inputs-[a-f0-9]{24}", volume or ""):
            raise OperationError("Runtime requires a staged conix-inputs-<token> volume.")

        if operation == "apply":
            rows = json.loads(self.run("volume", "inspect", volume, timeout=10, capture=True))
            if (len(rows) != 1
                or rows[0].get("Labels", {}).get("io.conix.input-token")
                    != volume.removeprefix("conix-inputs-")):
                raise OperationError("Selected input volume does not match its stage label.")

            for selected in (database, rpc):
                actual = self.run(
                    "network", "inspect", "--format", "{{.Name}}", selected,
                    timeout=10, capture=True,
                ).strip()
                if actual != selected:
                    raise OperationError(f"Selected network {selected!r} was not found on this Engine.")

        project = "conix-" + name
        compose = [
            "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
            "-p", project, "-f", str(ROOT / "compose/runtime.yml"),
        ]
        env = self.docker_environment.copy()
        env.update(
            CONIX_INDEXER_IMAGE=image,
            CONIX_INPUT_VOLUME=volume,
            CONIX_DATABASE_NETWORK=database,
            CONIX_RPC_NETWORK=rpc,
        )
        try:
            rendered = json.loads(self.run(
                *compose, "config", "--format", "json", env=env,
                timeout=20, capture=True,
            ))
            services = rendered["services"]
            service = services["indexer"]
            volumes = service.get("volumes", [])
            networks = rendered["networks"]
            if (
                set(services) != {"indexer"}
                or service.get("image") != image
                or service.get("pull_policy") != "never"
                or service.get("user") != "65532:65532"
                or service.get("read_only") is not True
                or service.get("privileged", False)
                or service.get("ports") or service.get("network_mode")
                or set(service.get("cap_drop", [])) != {"ALL"}
                or service.get("cap_add")
                or set(service.get("security_opt", [])) != {"no-new-privileges:true"}
                or set(service.get("networks", [])) != {"database", "rpc"}
                or service.get("devices") or service.get("device_cgroup_rules")
                or service.get("pid") or service.get("ipc") or service.get("uts")
                or len(volumes) != 1
                or volumes[0].get("type") != "volume"
                or volumes[0].get("source") not in ("inputs", volume)
                or volumes[0].get("target") != "/run/conix"
                or volumes[0].get("read_only") is not True
                or len(networks) != 2
                or networks["database"]["name"] != database
                or networks["rpc"]["name"] != rpc
            ):
                raise OperationError("Rendered runtime deployment violates image, input or confinement policy.")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise OperationError("Cannot validate the selected runtime deployment configuration.") from error

        if operation == "config/check":
            print(f"Verified constrained runtime Compose graph for {project} on selected Engine.")
            return

        if operation == "status":
            self.run(*compose, "ps", "--all", env=env, timeout=20)
            return

        if operation == "remove":
            self.run(*compose, "stop", "--timeout", "90", "indexer", env=env, timeout=105)
            self.run(*compose, "down", "--remove-orphans", env=env, timeout=60)
            print(f"Removed conix deployment project {project}; external inputs and networks were retained.")
            return

        if operation != "apply":
            raise OperationError("Deployment operation must be apply, remove, status or config/check.")

        self.verify_inputs(image, volume)
        self.run(*compose, "stop", "--timeout", "90", "indexer", env=env, timeout=105)
        try:
            self.run(*compose, "up", "--detach", "--no-build", "--pull",
                     "never", "--wait", "--wait-timeout", "30",
                     "indexer", env=env, timeout=60)
        except OperationError as error:
            raise OperationError(
                f"Conix project {project} did not confirm startup: {error}. "
                "Inspect the selected Engine and use deployment/status or "
                "deployment/remove with the same exact settings; inputs were retained."
            ) from error

        print(
            f"Applied conix project {project}; a running container is not evidence "
            "of chain freshness or payment readiness."
        )

    def test(self, image, images):
        """Run isolated PostgreSQL tests without adopting external resources."""
        if not IMAGE_ID.fullmatch(image):
            raise OperationError("test/postgres/run requires immutable RUNNER_IMAGE=sha256:<id>.")

        self.require_role(image, "test-runner")
        self.require_image(images["postgres"])
        project = "conix-tier0-" + secrets.token_hex(6)
        compose = [
            "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
            "-p", project, "-f", str(ROOT / "compose/tests.yml"),
        ]
        env = self.docker_environment.copy()
        env["CONIX_TEST_RUNNER_IMAGE"] = image
        env["CONIX_POSTGRES_IMAGE"] = images["postgres"]
        try:
            rendered = json.loads(self.run(*compose, "config", "--format", "json",
                                          env=env, timeout=20, capture=True))
            services = rendered["services"]
            networks = rendered["networks"]
            if (set(services) != {"tests", "postgres", "bootstrap", "initialize"}
                or services["tests"].get("image") != image
                or services["tests"].get("read_only") is not True
                or services["tests"].get("user") != "65532:65532"
                or services["postgres"].get("image") != images["postgres"]
                or services["postgres"].get("user") != "999:999"
                or services["postgres"].get("read_only") is not True
                or services["bootstrap"].get("image") != images["postgres"]
                or services["bootstrap"].get("user") != "999:999"
                or services["bootstrap"].get("read_only") is not True
                or services["initialize"].get("image") != image
                or services["initialize"].get("user") != "65532:65532"
                or services["initialize"].get("read_only") is not True
                or any(s.get("build") or s.get("ports") or s.get("privileged") or s.get("network_mode") for s in services.values())
                or any(any(v.get("type") == "bind" for v in s.get("volumes", [])) for s in services.values())
                or len(networks) != 1
                or not next(iter(networks.values())).get("internal")):
                raise OperationError("Rendered test services violate selected images, no-bind, network or confinement rules.")
        except (KeyError, ValueError) as error:
            raise OperationError("Cannot validate rendered test deployment.") from error

        failure = None
        try:
            # A successful, terminating bootstrap is deliberate; do not use
            # --abort-on-container-exit, which would stop tests as soon as
            # this one-shot migration completes.
            self.run(*compose, "up", "--detach", "--no-build", "--pull", "never",
                     "--wait", "--wait-timeout", "60", "postgres",
                     env=env, timeout=90)
            self.run(*compose, "run", "--rm", "--no-deps", "--pull",
                     "never", "bootstrap", env=env, timeout=60)
            self.run(*compose, "run", "--rm", "--no-deps", "--pull",
                     "never", "initialize", env=env, timeout=60)
            self.run(*compose, "run", "--rm", "--no-deps", "--pull",
                     "never", "tests", env=env, timeout=240)
        except Exception as error:
            failure = error

            # Capture bounded diagnostics before teardown, not after the
            # disposable container has been removed. These are fixture-only
            # services; never use this on a deployment with private inputs.
            try:
                self.run(*compose, "ps", "--all", env=env, timeout=15)
                self.run(*compose, "logs", "--no-color", "--tail", "35",
                         "postgres", "bootstrap", "initialize", "tests", env=env, timeout=15)
            except OperationError:
                pass
        finally:
            try:
                self.run(*compose, "down", "--volumes", "--remove-orphans",
                         "--timeout", "30", env=env, timeout=90)
            except Exception as cleanup:
                print(
                    f"Cleanup unconfirmed: project={project}, endpoint={self.docker_command_prefix[-1]}, "
                    f"compose={ROOT / 'compose/tests.yml'}. Inspect before removing.",
                    file=sys.stderr,
                )
                first = (
                    f" Original test failure: {failure}."
                    if isinstance(failure, OperationError) else
                    f" Original test failure: {type(failure).__name__}."
                    if failure is not None else ""
                )
                raise OperationError(f"Test project cleanup failed: {cleanup}.{first}") from cleanup

        if failure is not None:
            raise failure


def main():
    """Dispatch only the operation explicitly selected through Make or CLI."""
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
        "deployment/inputs/create", "deployment/inputs/remove",
        "database/schema/initialize",
        "deployment/config/check", "deployment/apply", "deployment/status", "deployment/remove",
    ))
    parser.add_argument("--wheelhouse")
    parser.add_argument("--image")
    parser.add_argument("--runner-image")
    parser.add_argument("--indexer-image")
    parser.add_argument("--config-file")
    parser.add_argument("--dsn-file")
    parser.add_argument("--owner-dsn-file")
    parser.add_argument("--volume")
    parser.add_argument("--deployment")
    parser.add_argument("--database-network")
    parser.add_argument("--rpc-network")
    args = parser.parse_args()

    try:
        if args.from_make:
            # Make exports simple, literally captured values via $(value VAR).
            # No operator text is ever interpolated into a shell recipe.
            raw = {key: os.environ.get("CONIX_MAKE_" + key, "") for key in (
                "DOCKER_CONTEXT", "WHEELHOUSE", "IMAGE", "RUNNER_IMAGE",
                "INDEXER_IMAGE", "CONFIG_FILE", "DSN_FILE",
                "OWNER_DSN_FILE", "INPUT_VOLUME", "DEPLOYMENT",
                "DATABASE_NETWORK", "RPC_NETWORK",
            )}

            args.context = raw["DOCKER_CONTEXT"]
            args.wheelhouse = raw["WHEELHOUSE"]
            args.image = raw["IMAGE"]
            args.runner_image = raw["RUNNER_IMAGE"]
            args.indexer_image = raw["INDEXER_IMAGE"]
            args.config_file = raw["CONFIG_FILE"]
            args.dsn_file = raw["DSN_FILE"]
            args.owner_dsn_file = raw["OWNER_DSN_FILE"]
            args.volume = raw["INPUT_VOLUME"]
            args.deployment = raw["DEPLOYMENT"]
            args.database_network = raw["DATABASE_NETWORK"]
            args.rpc_network = raw["RPC_NETWORK"]

        endpoint = select_endpoint(args.context, args.origin)
        with tempfile.TemporaryDirectory(prefix="conix-docker-client-") as directory:
            config = Path(directory)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}\n', encoding="utf-8")
            docker = Docker(endpoint, config)
            images = pinned_images()

            if args.action.startswith("image/") and args.action.endswith("/acquire"):
                docker.acquire_image(args.action.split("/")[1], images)

            elif args.action in ("wheelhouse/check", "wheelhouse/acquire"):
                from tools.wheelhouse.operations import SharedWheelhouse
                pool = SharedWheelhouse(docker, images, args.wheelhouse)
                selected = pool.check() if args.action.endswith("/check") else pool.acquire()
                print(f"Verified {len(selected)} hash-locked artifacts in WHEELHOUSE.",
                      file=sys.stderr)

            elif args.action == "test/postgres/run":
                docker.test(args.runner_image or "", images)

            elif args.action == "image/indexer/check":
                docker.check_indexer(args.image)

            elif args.action == "image/evm-test-runner/check":
                docker.check_evm_test_runner(args.image)

            elif args.action in ("deployment/inputs/create", "test/input-staging/run"):
                docker.stage_inputs(args.indexer_image, args.config_file, args.dsn_file,
                                    proof=args.action == "test/input-staging/run")

            elif args.action == "deployment/inputs/remove":
                docker.remove_inputs(args.volume)

            elif args.action == "database/schema/initialize":
                docker.initialize_schema(
                    args.indexer_image, args.config_file, args.owner_dsn_file,
                    args.database_network,
                )

            elif args.action.startswith("deployment/"):
                docker.deployment(
                    operation=args.action.removeprefix("deployment/"),
                    name=args.deployment, image=args.indexer_image,
                    volume=args.volume, database=args.database_network,
                    rpc=args.rpc_network,
                )

            else:
                target = args.action.split("/")[1]
                from tools.wheelhouse.operations import SharedWheelhouse
                print(SharedWheelhouse(docker, images, args.wheelhouse).build(target))
    except (OperationError, PoolError, OSError) as error:
        raise SystemExit(f"Conix container operation failed ({type(error).__name__}): {error}") from None


if __name__ == "__main__":
    main()
