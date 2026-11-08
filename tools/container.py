"""One explicitly selected local Docker Engine for each container operation.

Host-side orchestration only. Runtime code never receives the Docker socket.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import tomllib
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
    if os.geteuid() == 0:
        raise OperationError(
            "Conix container operations refuse host UID 0. Use the selected "
            "Docker Engine's unprivileged operator account, not sudo."
        )
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

    def require_image(self, reference):
        """Check exact local image identity without printing JSON into Make output."""
        identity = self.run(
            "image", "inspect", "--format", "{{.Id}}", reference,
            timeout=10, capture=True,
        ).strip()
        if not IMAGE_ID.fullmatch(identity):
            raise OperationError("Selected image is not a valid local image ID.")
        return identity

    def require_rootless_engine(self):
        """A local socket is not proof that the selected daemon is rootless."""
        try:
            options = json.loads(self.run(
                "info", "--format", "{{json .SecurityOptions}}",
                timeout=10, capture=True,
            ))
        except (ValueError, TypeError) as error:
            raise OperationError(
                "Cannot establish selected Docker Engine rootless status from daemon security options."
            ) from error
        if type(options) is not list or not all(type(value) is str for value in options):
            raise OperationError("Selected Docker Engine returned malformed security options.")
        if "name=rootless" not in options:
            raise OperationError(
                "Runtime start requires a verified rootless Docker Engine; "
                "a local Unix socket alone does not qualify a rootful daemon."
            )

    def prepare(self, images):
        """Networked, explicit image acquisition; never part of a test run."""
        for reference in (
            images["python_base"],
            images["postgres"],
            "docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e",
        ):
            self.run("pull", "--platform", "linux/amd64", reference, timeout=180)

    def build(self, target, images, wheelhouse_image=None):
        self.require_image(images["python_base"])
        if target == "wheelhouse":
            dockerfile, context, network = "containers/wheelhouse/Dockerfile", ".", "default"
            kwargs = ()
        else:
            if not IMAGE_ID.fullmatch(wheelhouse_image or ""):
                raise OperationError("WHEELHOUSE_IMAGE must name a prepared immutable local image ID.")
            if self.require_image(wheelhouse_image) != wheelhouse_image:
                raise OperationError("Selected wheelhouse image identity changed.")
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
        self.require_image(image)
        self.run("run", "--rm", "--network", "none", "--read-only",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                 "--pids-limit", "64", "--memory", "256m",
                 "--user", "65532:65532", image, "--help", timeout=30)

    def private_bytes(self, value, max_size, *, secret):
        path = Path(value)
        if not path.is_absolute():
            raise OperationError("Staged input files must use absolute host paths.")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OperationError("Staged input must be an operator-owned regular file.")
            if secret and info.st_mode & 0o077:
                raise OperationError("Database credential source must have no group/other permissions.")
            if not secret and info.st_mode & 0o022:
                raise OperationError("Conix settings source must not be group/other writable.")
            data = os.read(fd, max_size + 1)
        finally:
            os.close(fd)
        if not 0 < len(data) <= max_size:
            raise OperationError("Staged input file is empty or exceeds its size limit.")
        return data

    def stage_inputs(self, image, config_path, dsn_path, *, proof=False):
        """Create and own a unique input volume, with no host bind mounts."""
        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("CONIX_IMAGE must select a prepared immutable runtime image.")
        self.require_image(image)
        config = self.private_bytes(config_path, 64 * 1024, secret=False)
        dsn = self.private_bytes(dsn_path, 8192, secret=True)
        try:
            data = tomllib.loads(config.decode("utf-8"))
            if data["database"]["dsn_file"] != "/run/conix/connection.dsn":
                raise ValueError
        except (UnicodeError, KeyError, TypeError, ValueError) as error:
            raise OperationError(
                "Stage settings with database.dsn_file=/run/conix/connection.dsn; no host path is mounted."
            ) from error
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
            argv = self.base + [
                "run", "--rm", "--interactive", "--network", "none",
                "--read-only", "--user", "0:0", "--cap-drop", "ALL",
                "--cap-add", "CHOWN", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "32", "--memory", "128m",
                "--mount", f"type=volume,source={volume},target=/run/conix",
                "--entrypoint", "python", image, "-m", "conix.input_staging",
            ]
            result = subprocess.run(argv, input=envelope, capture_output=True,
                                    cwd=ROOT, env=self.env, timeout=40, check=False)
            if result.returncode:
                raise OperationError("Conix input staging rejected the private inputs or volume; no secret was logged.")
            self.verify_inputs(image, volume)
            if not proof:
                remove = False
                print(volume)
        except Exception:
            remove = True
            raise
        finally:
            if remove:
                try:
                    self.remove_inputs(volume, token=token)
                except Exception as error:
                    raise OperationError(
                        f"Cannot confirm cleanup of owned private volume {volume}; inspect selected Engine {self.base[-1]}."
                    ) from error

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
            "run", "--rm", "--network", "none", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "128m",
            "--mount", f"type=volume,source={volume},target=/run/conix,readonly",
            "--entrypoint", "python", image, "-c", check,
            timeout=30,
        )

    def remove_inputs(self, volume, *, token=None):
        if not re.fullmatch(r"conix-inputs-[a-f0-9]{24}", volume or ""):
            raise OperationError("Only conix-owned input-volume identities can be removed.")
        values = json.loads(self.run("volume", "inspect", volume, timeout=10, capture=True))
        if len(values) != 1 or values[0].get("Labels", {}).get("io.conix.input-token") != (token or volume.removeprefix("conix-inputs-")):
            raise OperationError("Private input-volume label does not match its identity; refusing removal.")
        self.run("volume", "rm", volume, timeout=30)

    def deployment(self, *, operation, name, image, volume, database, rpc):
        """Render/admit one instance, then use that exact project on this Engine."""
        for label, value in (
            ("deployment", name), ("database network", database), ("RPC network", rpc),
        ):
            if type(value) is not str or not CONTEXT_NAME.fullmatch(value):
                raise OperationError(f"Select one valid {label} name without paths or control characters.")
        if database == rpc:
            raise OperationError("Source RPC and database networks must be separately selected.")
        if not IMAGE_ID.fullmatch(image or ""):
            raise OperationError("Runtime requires an immutable CONIX_IMAGE=sha256:<id>.")
        # Resource existence is a start precondition, not a stop/status
        # precondition. Teardown must work after image, volume, network or
        # source files have disappeared from this Engine.
        if operation == "start":
            self.require_rootless_engine()
            self.require_image(image)
        # An input volume's identity and ownership label must agree before a
        # command can reuse it. The label is not a credential or permission.
        if not re.fullmatch(r"conix-inputs-[a-f0-9]{24}", volume or ""):
            raise OperationError("Runtime requires a staged conix-inputs-<token> volume.")
        if operation == "start":
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
        env = self.env.copy()
        env.update(
            CONIX_IMAGE=image,
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

        if operation == "status":
            self.run(*compose, "ps", "--all", env=env, timeout=20)
            return
        if operation == "stop":
            self.run(*compose, "stop", "--timeout", "90", "indexer", env=env, timeout=105)
            self.run(*compose, "down", "--remove-orphans", env=env, timeout=60)
            print(f"Stopped project {project}; external inputs and networks were retained.")
            return
        if operation != "start":
            raise OperationError("Runtime operation must be start, stop or status.")
        self.verify_inputs(image, volume)
        self.run(*compose, "stop", "--timeout", "90", "indexer", env=env, timeout=105)
        try:
            self.run(*compose, "up", "--detach", "--no-build", "--pull",
                     "never", "--wait", "--wait-timeout", "30",
                     "indexer", env=env, timeout=60)
        except OperationError:
            raise OperationError(
                f"Conix project {project} did not confirm startup. Inspect the "
                "selected Engine and use runtime/status or runtime/stop with "
                "the same exact settings; inputs were retained."
            ) from None
        print(
            f"Started conix project {project}; a running container is not evidence "
            "of chain freshness or payment readiness."
        )

    def test(self, image, images):
        if not IMAGE_ID.fullmatch(image):
            raise OperationError("CONIX_TEST_IMAGE must be an immutable sha256 image ID.")
        self.run("image", "inspect", image, timeout=10)
        self.require_image(images["postgres"])
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
            networks = rendered["networks"]
            if (set(services) != {"tests", "postgres"}
                or services["tests"].get("image") != image
                or services["tests"].get("read_only") is not True
                or services["tests"].get("user") != "65532:65532"
                or services["postgres"].get("image") != images["postgres"]
                or services["postgres"].get("user") != "999:999"
                or services["postgres"].get("read_only") is not True
                or any(s.get("ports") or s.get("privileged") or s.get("network_mode") for s in services.values())
                or any(any(v.get("type") == "bind" for v in s.get("volumes", [])) for s in services.values())
                or len(networks) != 1
                or not next(iter(networks.values())).get("internal")):
                raise OperationError("Rendered test services violate selected images, no-bind, network or confinement rules.")
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
    parser.add_argument("--from-make", action="store_true")
    parser.add_argument("--context", default="")
    parser.add_argument("--origin", default="undefined")
    parser.add_argument("action", choices=(
        "deps/images/prepare", "deps/wheelhouse/build", "image/build",
        "image/smoke", "tests/image/build", "tests/container",
        "inputs/stage", "inputs/proof", "inputs/remove",
        "runtime/start", "runtime/stop", "runtime/status",
    ))
    parser.add_argument("--test-image")
    parser.add_argument("--wheelhouse-image")
    parser.add_argument("--image")
    parser.add_argument("--config-file")
    parser.add_argument("--dsn-file")
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
                "DOCKER_CONTEXT", "WHEELHOUSE_IMAGE", "CONIX_IMAGE",
                "CONIX_TEST_IMAGE", "CONIX_CONFIG_FILE", "CONIX_DSN_FILE",
                "CONIX_INPUT_VOLUME", "CONIX_DEPLOYMENT",
                "CONIX_DATABASE_NETWORK", "CONIX_RPC_NETWORK",
            )}
            args.context = raw["DOCKER_CONTEXT"]
            args.wheelhouse_image = raw["WHEELHOUSE_IMAGE"]
            args.image = raw["CONIX_IMAGE"]
            args.test_image = raw["CONIX_TEST_IMAGE"]
            args.config_file = raw["CONIX_CONFIG_FILE"]
            args.dsn_file = raw["CONIX_DSN_FILE"]
            args.volume = raw["CONIX_INPUT_VOLUME"]
            args.deployment = raw["CONIX_DEPLOYMENT"]
            args.database_network = raw["CONIX_DATABASE_NETWORK"]
            args.rpc_network = raw["CONIX_RPC_NETWORK"]
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
            elif args.action in ("inputs/stage", "inputs/proof"):
                docker.stage_inputs(args.image, args.config_file, args.dsn_file,
                                    proof=args.action == "inputs/proof")
            elif args.action == "inputs/remove":
                docker.remove_inputs(args.volume)
            elif args.action.startswith("runtime/"):
                docker.deployment(
                    operation=args.action.removeprefix("runtime/"),
                    name=args.deployment, image=args.image,
                    volume=args.volume, database=args.database_network,
                    rpc=args.rpc_network,
                )
            else:
                target = "runtime" if args.action == "image/build" else "tests"
                print(docker.build(target, images, args.wheelhouse_image))
    except (OperationError, OSError) as error:
        raise SystemExit(f"Conix container operation refused or failed: {error}") from None


if __name__ == "__main__":
    main()
