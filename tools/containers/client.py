"""Choose one local Docker engine and run sanitized bounded CLI commands.

This layer does not choose Conix images, input volumes, databases or
application lifecycles. The caller supplies its own working directory.
"""

import json
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

CONTEXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")

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


_PUBLIC_WORKER_LINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,:;()/'=+-]{0,239}\Z")


def _public_worker_explanation(stderr):
    """Preserve one safe worker cause, never raw container stderr.

    Network-enabled acquisition runs a published tool, but its output may
    contain remote response material or ambient credentials. Reject any
    line with control characters, private markers or key-shaped data.
    """
    if type(stderr) is not str:
        return None

    for line in reversed(stderr.splitlines()):
        if (
            1 <= len(line) <= 240
            and _PUBLIC_WORKER_LINE.fullmatch(line) is not None
            and not any(marker in line.lower() for marker in (
                "private", "password", "secret", "token", "authorization",
                "cookie", "bearer", "dsn", "://",
            ))
            and re.search(r"(?:0x)?[0-9a-fA-F]{64}", line) is None
        ):
            return line

    return None


def command(argv, *, env, working_dir, timeout=60, capture=False, progress_to_stderr=False, public_diagnostic=False):
    """Run a bounded Docker command; only explicitly admitted diagnostics are public."""
    captures_output = capture or public_diagnostic

    try:
        completed_process = subprocess.run(
            argv,
            cwd=working_dir,
            env=env,
            timeout=timeout,
            check=False,
            text=captures_output,
            capture_output=captures_output,
            stdout=None if captures_output else (sys.stderr if progress_to_stderr else None),
        )
    except subprocess.TimeoutExpired as error:
        # The Docker CLI timed out; its daemon may still be performing work.
        raise OperationError(
            f"{_docker_operation(argv)} client timed out after {timeout}s; "
            "the daemon-side effect is unconfirmed."
        ) from error
    except OSError as error:
        # Errno is safe to report even when Docker argv contains private data.
        raise OperationError(
            f"{_docker_operation(argv)} could not start "
            f"({type(error).__name__}, errno={error.errno}); "
            "no successful client result was observed."
        ) from error

    if completed_process.returncode:
        # Ordinary Docker output can contain private fixture data. Only a
        # deliberately public worker diagnostic is eligible for display.
        if public_diagnostic:
            admitted_explanation = _public_worker_explanation(completed_process.stderr)

            raise OperationError(
                f"Contained public worker failed (exit {completed_process.returncode}): "
                + (admitted_explanation or "no admitted nonsecret diagnostic returned."),
                exit_code=completed_process.returncode,
            )

        raise OperationError(
            f"{_docker_operation(argv)} failed (exit {completed_process.returncode}).",
            exit_code=completed_process.returncode,
        )

    if capture and len(completed_process.stdout) > 65536:
        raise OperationError("Docker returned an oversized response.")

    return completed_process.stdout if capture else None


def select_endpoint(name, origin, *, working_dir):
    """Select an explicit local Engine or prompt the operator at a terminal."""
    if os.geteuid() == 0:
        raise OperationError(
            "Docker operations refuse host root (UID 0). Use the selected "
            "Engine's unprivileged operator account, not sudo."
        )

    context_listing = command(
        ["docker", "context", "ls", "--format", "{{.Name}}\t{{.DockerEndpoint}}"],
        env=environment(),
        working_dir=working_dir,
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
            "Cannot access the local Docker context selection prompt "
            f"({type(error).__name__}, errno={error.errno}); "
            "pass DOCKER_CONTEXT=<name> on the command line. "
            f"Local contexts: {', '.join(sorted(local_endpoints))}."
        ) from error

    if not selected_number.isascii() or not selected_number.isdecimal():
        raise OperationError("Invalid context choice. No Docker Engine was selected.")

    selected_index = int(selected_number)
    if str(selected_index) != selected_number or not 1 <= selected_index <= len(context_names):
        raise OperationError(f"{selected_number = !r} is not a listed Docker context choice.")

    return local_endpoints[context_names[selected_index - 1]]


class DockerClient:
    """Pin an admitted engine and its private Docker client configuration."""

    def __init__(self, endpoint, config_dir, *, working_dir):
        self.endpoint = endpoint
        self.config_dir = config_dir
        self.working_dir = working_dir
        self.docker_command_prefix = ["docker", "--config", str(config_dir), "--host", endpoint]
        self.docker_environment = environment(config_dir)

    def run(self, *args, timeout=60, capture=False, env=None, progress_to_stderr=False, public_diagnostic=False):
        return command(
            self.docker_command_prefix + list(args),
            env=self.docker_environment if env is None else env,
            working_dir=self.working_dir,
            timeout=timeout,
            capture=capture,
            progress_to_stderr=progress_to_stderr,
            public_diagnostic=public_diagnostic,
        )

    def require_image(self, reference):
        """Check the exact local image ID without publishing the image metadata."""
        actual_image_id = self.run(
            "image", "inspect", "--format", "{{.Id}}", reference,
            timeout=10,
            capture=True,
        ).strip()

        if not IMAGE_ID.fullmatch(actual_image_id):
            raise OperationError("Selected image is not a valid local image ID.")

        return actual_image_id

    def require_rootless_engine(self):
        """The local socket alone does not establish rootless daemon identity."""
        try:
            security_options = json.loads(
                self.run(
                    "info", "--format", "{{json .SecurityOptions}}",
                    timeout=10,
                    capture=True,
                )
            )
        except (ValueError, TypeError) as error:
            raise OperationError("Cannot establish selected Docker Engine rootless status from daemon security options.") from error

        if type(security_options) is not list or not all(type(option) is str for option in security_options):
            raise OperationError("Selected Docker Engine returned malformed security options.")

        if "name=rootless" not in security_options:
            raise OperationError(
                "Runtime start requires a verified rootless Docker Engine; "
                "a local Unix socket alone does not qualify a rootful daemon."
            )

