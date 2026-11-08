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

def pinned_images():
    document = json.loads((ROOT / "containers/images.json").read_text(encoding="utf-8"))
    if set(document) != {"python_base", "postgres"}:
        raise OperationError("Reviewed image manifest must contain only python_base and postgres.")
    for role, name in document.items():
        if (type(name) is not str or "@sha256:" not in name
                or not IMAGE_ID.fullmatch("sha256:" + name.rsplit("@sha256:", 1)[-1])):
            raise OperationError(f"Reviewed {role} base image must have a complete sha256 pin.")
    return document




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


def command(argv, *, env, timeout=60, capture=False, progress_to_stderr=False):
    try:
        result = subprocess.run(argv, cwd=ROOT, env=env, timeout=timeout,
                                text=capture, capture_output=capture,
                                stdout=sys.stderr if progress_to_stderr else None,
                                check=False)
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

    def run(self, *args, timeout=60, capture=False, env=None, progress_to_stderr=False):
        return command(self.base + list(args), env=self.env if env is None else env,
                       timeout=timeout, capture=capture,
                       progress_to_stderr=progress_to_stderr)

    def prepare(self, images):
        """Networked, explicit image acquisition; never part of a test run."""
        for reference in (
            images["python_base"],
            images["postgres"],
            "docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e",
        ):
            self.run("pull", "--platform", "linux/amd64", reference, timeout=180)

    def build(self, target, images, wheelhouse_image=None):
        self.run("image", "inspect", images["python_base"], timeout=10)
        if target == "wheelhouse":
            dockerfile, context, network = "containers/wheelhouse/Dockerfile", ".", "default"
            kwargs = ()
        else:
            if not IMAGE_ID.fullmatch(wheelhouse_image or ""):
                raise OperationError("WHEELHOUSE_IMAGE must name a prepared immutable local image ID.")
            self.run("image", "inspect", wheelhouse_image, timeout=10)
            # Dockerfile FROM consumes a local content-labelled tag, not the
            # operator's mutable 'latest'. Verify this source by ID first;
            # pip additionally verifies every selected wheel against the lock.
            source = "conix-wheelhouse:sha-" + wheelhouse_image[7:27]
            self.run("tag", wheelhouse_image, source, timeout=10)
            dockerfile, context, network = "Dockerfile", ".", "none"
            kwargs = ("--build-arg", "WHEELHOUSE_IMAGE=" + source)
        with tempfile.TemporaryDirectory(prefix="conix-image-") as directory:
            iid = Path(directory) / "iid"
            self.run(
                "build", "--pull=false", "--platform", "linux/amd64",
                "--network", network, "--file", dockerfile,
                "--build-arg", "PYTHON_BASE_IMAGE=" + images["python_base"],
                *kwargs, "--iidfile", str(iid),
                "--tag", f"conix-tier0-{target}:local", context,
                timeout=600, progress_to_stderr=True,
            )
            result = iid.read_text(encoding="ascii").strip()
        if not IMAGE_ID.fullmatch(result):
            raise OperationError("The image build did not provide an immutable image ID.")
        return result

    def smoke(self, image):
        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("CONIX_IMAGE must select an immutable local image ID.")
        self.run("image", "inspect", image, timeout=10)
        self.run("run", "--rm", "--network", "none", "--read-only",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                 "--pids-limit", "64", "--memory", "256m",
                 "--user", "65532:65532", image, "--help", timeout=30)

    def test(self, image, images):
        if not IMAGE_ID.fullmatch(image):
            raise OperationError("CONIX_TEST_IMAGE must be an immutable sha256 image ID.")
        self.run("image", "inspect", image, timeout=10)
        self.run("image", "inspect", images["postgres"], timeout=10)
        project = "conix-tier0-" + secrets.token_hex(6)
        compose = [
            "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
            "-p", project, "-f", str(ROOT / "compose/tests.yml"),
        ]
        env = self.env.copy()
        env["CONIX_TEST_IMAGE"] = image
        env["CONIX_POSTGRES_IMAGE"] = images["postgres"]
        try:
            rendered = json.loads(self.run(*compose, "config", "--format", "json",
                                          env=env, timeout=20, capture=True))
            services = rendered["services"]
            if (services["tests"].get("image") != image
                or services["tests"].get("read_only") is not True
                or services["tests"].get("user") != "65532:65532"
                or services["postgres"].get("image") != images["postgres"]
                or services["postgres"].get("read_only") is not True
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
            # Capture bounded diagnostics before teardown, not after the
            # disposable container has been removed. These are fixture-only
            # services; never use this on a deployment with private inputs.
            try:
                self.run(*compose, "ps", "--all", env=env, timeout=15)
                self.run(*compose, "logs", "--no-color", "--tail", "35",
                         "postgres", "tests", env=env, timeout=15)
            except OperationError:
                pass
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
    parser.add_argument("action", choices=(
        "deps/images/prepare", "deps/wheelhouse/build", "image/build",
        "image/smoke", "tests/image/build", "tests/container",
    ))
    parser.add_argument("--test-image")
    parser.add_argument("--wheelhouse-image")
    parser.add_argument("--image")
    args = parser.parse_args()
    try:
        endpoint = select_endpoint(args.context, args.origin)
        with tempfile.TemporaryDirectory(prefix="conix-docker-client-") as directory:
            config = Path(directory)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}\n', encoding="utf-8")
            docker = Docker(endpoint, config)
            images = pinned_images()
            if args.action == "deps/images/prepare":
                docker.prepare(images)
            elif args.action == "deps/wheelhouse/build":
                print(docker.build("wheelhouse", images))
            elif args.action == "tests/container":
                docker.test(args.test_image or "", images)
            elif args.action == "image/smoke":
                docker.smoke(args.image)
            else:
                target = "runtime" if args.action == "image/build" else "tests"
                print(docker.build(target, images, args.wheelhouse_image))
    except (OperationError, OSError) as error:
        raise SystemExit(f"Conix container operation refused or failed: {error}") from None


if __name__ == "__main__":
    main()
