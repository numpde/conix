"""One explicitly selected local Docker Engine for each container operation.

Host-side orchestration only. Runtime code never receives the Docker socket.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
CONTEXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")


class OperationError(Exception):
    pass


def local_socket(endpoint):
    try:
        uri = urlsplit(endpoint)
        return (uri.scheme == "unix" and not uri.netloc and not uri.query
                and not uri.fragment and uri.path.startswith("/")
                and uri.path != "/" and len(endpoint) <= 4096
                and not any(ord(ch) < 32 for ch in endpoint))
    except ValueError:
        return False


def environment(client_config=None):
    # Ambient client/Compose overrides are not authority to select an Engine,
    # Compose file, project, profile or build path.
    result = {k: v for k, v in os.environ.items()
              if not k.startswith(("DOCKER_", "COMPOSE_"))}
    result["COMPOSE_DISABLE_ENV_FILE"] = "1"
    result["DOCKER_BUILDKIT"] = "1"
    if client_config is not None:
        result["DOCKER_CONFIG"] = str(client_config)
    return result


def command(argv, *, env, timeout=60, capture=False):
    try:
        result = subprocess.run(argv, cwd=ROOT, env=env, timeout=timeout,
                                text=capture, capture_output=capture, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OperationError(f"Command failed to run: {argv[-1]} ({type(error).__name__}).") from error
    if result.returncode:
        raise OperationError(f"Docker operation {argv[-1]} failed (exit {result.returncode}).")
    if capture and len(result.stdout) > 65536:
        raise OperationError("Docker returned an oversized response.")
    return result.stdout if capture else None


def select_endpoint(name, origin):
    listing = command(
        ["docker", "context", "ls", "--format", "{{.Name}}\t{{.DockerEndpoint}}"],
        env=environment(), timeout=10, capture=True,
    )
    contexts = {}
    for line in listing.splitlines():
        key, sep, endpoint = line.partition("\t")
        if not sep or not CONTEXT_NAME.fullmatch(key) or key in contexts:
            raise OperationError("Docker returned malformed or duplicate context entries.")
        contexts[key] = endpoint
    available = {key: endpoint for key, endpoint in contexts.items()
                 if local_socket(endpoint)}
    if origin == "command line":
        if not CONTEXT_NAME.fullmatch(name):
            raise OperationError("Select one named local Docker context on the Make command line.")
        if name not in available:
            raise OperationError(f"Context {name!r} is missing or does not use a local Unix socket.")
        return available[name]
    if not available:
        raise OperationError("Docker does not list a usable local Unix-socket context.")
    try:
        with open("/dev/tty", "r") as reader, open("/dev/tty", "w") as writer:
            keys = sorted(available)
            writer.write("Choose a local Docker context (none selected automatically):\n")
            for i, key in enumerate(keys, 1):
                writer.write(f"  {i}) {key}\n")
            writer.write("Context number: ")
            writer.flush()
            answer = reader.readline().strip()
    except OSError as error:
        raise OperationError(
            "No terminal. Pass DOCKER_CONTEXT=<name> on the Make command line. "
            f"Local contexts: {', '.join(sorted(available))}."
        ) from error
    if not answer.isascii() or not answer.isdecimal() or str(int(answer)) != answer or not 1 <= int(answer) <= len(keys):
        raise OperationError("Invalid context choice. No Docker Engine was selected.")
    return available[keys[int(answer) - 1]]


class Docker:
    def __init__(self, endpoint, config_dir):
        self.base = ["docker", "--config", str(config_dir), "--host", endpoint]
        self.env = environment(config_dir)

    def run(self, *args, timeout=60, capture=False, env=None):
        return command(self.base + list(args), env=self.env if env is None else env,
                       timeout=timeout, capture=capture)

    def build(self, target):
        with tempfile.TemporaryDirectory(prefix="conix-image-") as directory:
            iid = Path(directory) / "iid"
            self.run("build", "--target", target, "--iidfile", str(iid),
                     "--tag", f"conix-tier0-{target}:local", ".", timeout=600)
            result = iid.read_text(encoding="ascii").strip()
        if not IMAGE_ID.fullmatch(result):
            raise OperationError("The image build did not provide an immutable image ID.")
        return result

    def test(self, image):
        if not IMAGE_ID.fullmatch(image):
            raise OperationError("CONIX_TEST_IMAGE must be an immutable sha256 image ID.")
        self.run("image", "inspect", image, timeout=10)
        project = "conix-tier0-" + secrets.token_hex(6)
        compose = [
            "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
            "-p", project, "-f", str(ROOT / "compose/tests.yml"),
        ]
        env = self.env.copy()
        env["CONIX_TEST_IMAGE"] = image
        try:
            rendered = json.loads(self.run(*compose, "config", "--format", "json",
                                          env=env, timeout=20, capture=True))
            services = rendered["services"]
            if (services["tests"].get("image") != image
                or services["tests"].get("read_only") is not True
                or services["tests"].get("user") != "65532:65532"
                or any(s.get("ports") for s in services.values())):
                raise OperationError("Rendered test services violate the selected image or confinement.")
        except (KeyError, ValueError) as error:
            raise OperationError("Cannot validate rendered test deployment.") from error
        failure = None
        try:
            self.run(*compose, "up", "--no-build", "--pull", "never",
                     "--abort-on-container-exit", "--exit-code-from", "tests",
                     env=env, timeout=240)
        except Exception as error:
            failure = error
        finally:
            try:
                self.run(*compose, "down", "--volumes", "--remove-orphans",
                         "--timeout", "30", env=env, timeout=90)
            except Exception as cleanup:
                print(
                    f"Cleanup unconfirmed: project={project}, endpoint={self.base[-1]}, "
                    f"compose={ROOT / 'compose/tests.yml'}. Inspect before removing.",
                    file=sys.stderr,
                )
                raise OperationError(f"Test project cleanup failed: {cleanup}") from failure
        if failure is not None:
            raise failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="")
    parser.add_argument("--origin", default="undefined")
    parser.add_argument("action", choices=("image/build", "tests/image/build", "tests/container"))
    parser.add_argument("--test-image")
    args = parser.parse_args()
    try:
        endpoint = select_endpoint(args.context, args.origin)
        with tempfile.TemporaryDirectory(prefix="conix-docker-client-") as directory:
            config = Path(directory)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}\n', encoding="utf-8")
            docker = Docker(endpoint, config)
            if args.action == "tests/container":
                docker.test(args.test_image or "")
            else:
                print(docker.build("runtime" if args.action == "image/build" else "tests"))
    except (OperationError, OSError) as error:
        raise SystemExit(f"Conix container operation refused or failed: {error}") from None


if __name__ == "__main__":
    main()
