"""Named Conix deployments: configuration, preparation, and application lifecycle.

The saved declaration selects resources once. A prepared receipt contains only
immutable image/volume identities; credentials stay in operator-owned files.
One command never silently performs another command's privileged work.
"""

from dataclasses import dataclass
import fcntl
import json
import os
import sys
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib
from contextlib import contextmanager

from tools.containers.client import CONTEXT_NAME, IMAGE_ID, OperationError, select_endpoint
from tools.containers.history import VOLUME_NAME, _admit_network, _admit_volume

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENTS = ROOT / "config" / "deployments"
NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
PREPARED = "prepared.json"
FIELDS = {
    "docker_context": "context",
    "source_config": "config_file",
    "runtime_dsn_file": "dsn_file",
    "owner_dsn_file": "owner_dsn_file",
    "database_network": "database_network",
    "rpc_network": "rpc_network",
    "wheelhouse": "wheelhouse",
}


@dataclass(frozen=True)
class Declaration:
    name: str
    docker_context: str
    source_config: str
    runtime_dsn_file: str
    owner_dsn_file: str
    database_network: str
    rpc_network: str
    wheelhouse: str


def _directory(name):
    if type(name) is not str or NAME.fullmatch(name) is None:
        raise OperationError("Select DEPLOYMENT=<lowercase-name> without path separators.")
    return DEPLOYMENTS / name


def _private_file(path):
    try:
        info = path.lstat()
    except OSError as error:
        raise OperationError("Selected named deployment file is unavailable.") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise OperationError("Named deployment files must be owner-only regular files (0600).")
    return path


def _validate(decl):
    if CONTEXT_NAME.fullmatch(decl.docker_context) is None:
        raise OperationError("The saved Docker context name is invalid.")
    for path in (decl.source_config, decl.runtime_dsn_file, decl.owner_dsn_file, decl.wheelhouse):
        if not Path(path).is_absolute() or len(path) > 4096:
            raise OperationError("Saved host input paths must be absolute and bounded.")
    for name in (decl.database_network, decl.rpc_network):
        if CONTEXT_NAME.fullmatch(name) is None:
            raise OperationError("Saved Docker network names must be explicit and valid.")
    if decl.database_network == decl.rpc_network:
        raise OperationError("Selected database and RPC networks must be distinct.")


def _write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _write_prepared(folder, record):
    descriptor, temporary = tempfile.mkstemp(prefix=".prepared-", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / PREPARED)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(name):
    folder = _directory(name)
    try:
        details = folder.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
            raise OperationError("Named deployment directory must be owner-only (0700).")
        data = _private_file(folder / "deployment.toml").read_bytes()
        if len(data) > 8192:
            raise OperationError("Named deployment configuration exceeds its size bound.")
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, OSError) as error:
        raise OperationError("Cannot read the selected named deployment configuration.") from error
    if type(document) is not dict or set(document) != {"version", *FIELDS} or document["version"] != 1:
        raise OperationError("Saved deployment configuration has unexpected fields or version.")
    if any(type(document[key]) is not str or not document[key] for key in FIELDS):
        raise OperationError("Saved deployment configuration has a missing or malformed selection.")
    decl = Declaration(name=name, **{key: document[key] for key in FIELDS})
    _validate(decl)
    return decl


def _prompt(label):
    try:
        with open("/dev/tty", "r", encoding="utf-8") as source, open("/dev/tty", "w", encoding="utf-8") as sink:
            sink.write(label + ": ")
            sink.flush()
            return source.readline().rstrip("\r\n")
    except OSError as error:
        raise OperationError(
            f"Missing {label} for deployment/init; supply it explicitly on the Make command line."
        ) from error


def init(name, options):
    """Record an immutable, nonsecret deployment selection, without Docker."""
    folder = _directory(name)
    if os.geteuid() == 0:
        raise OperationError("Create named deployments as the unprivileged Engine operator, not root.")
    if getattr(options, "origin", None) != "command line":
        raise OperationError("deployment/init requires explicit DOCKER_CONTEXT=<local-context>.")
    values = {}
    for (field, option) in FIELDS.items():
        value = getattr(options, option, None)
        if not value:
            value = _prompt(option.upper())
        values[field] = value
    decl = Declaration(name=name, **values)
    _validate(decl)
    if folder.exists() or folder.is_symlink():
        raise OperationError("Named deployment already exists; initialization will not overwrite it.")

    for path in (ROOT / "config", DEPLOYMENTS):
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
                raise OperationError("Deployment configuration parent is not an owned directory.")
        else:
            path.mkdir(mode=0o700)

    folder.mkdir(mode=0o700)
    # JSON quoted strings are valid TOML basic strings; keys are fixed source text.
    content = "version = 1\n" + "".join(
        f"{key} = {json.dumps(getattr(decl, key), ensure_ascii=False)}\n" for key in FIELDS
    )
    _write_new(folder / "deployment.toml", content.encode("utf-8"))
    print(f"Initialized deployment {name} (no image, secret, database, or container created).")
    print(f"Next: make deployment/prepare DEPLOYMENT={name}")


@contextmanager
def _locked(name):
    """Serialize modifying operations on this named deployment."""
    folder = _directory(name)
    path = folder / ".operator.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OperationError("Another operation owns this named deployment.") from error
        yield
    finally:
        os.close(fd)


def _prepared(decl):
    path = _directory(decl.name) / PREPARED
    try:
        data = _private_file(path).read_bytes()
        if len(data) > 4096:
            raise OperationError("Prepared deployment receipt exceeds its bound.")
        receipt = json.loads(data)
    except (ValueError, OSError) as error:
        raise OperationError(
            f"No usable prepared deployment receipt; run make deployment/prepare DEPLOYMENT={decl.name}."
        ) from error
    if (type(receipt) is not dict or set(receipt) != {"version", "image", "volume"}
            or type(receipt["version"]) is not int or receipt["version"] != 1
            or type(receipt["image"]) is not str or IMAGE_ID.fullmatch(receipt["image"]) is None
            or type(receipt["volume"]) is not str or VOLUME_NAME.fullmatch(receipt["volume"]) is None):
        raise OperationError("The selected prepared deployment receipt is invalid.")
    return receipt


def _docker(decl, client_cls):
    endpoint = select_endpoint(decl.docker_context, "command line", working_dir=ROOT)
    config_dir = tempfile.TemporaryDirectory(prefix="conix-named-docker-")
    (Path(config_dir.name) / "config.json").write_text('{"auths":{},"proxies":{}}\n')
    return (client_cls(endpoint, Path(config_dir.name)), config_dir)


def _inspect_existing(docker, name):
    from tools.containers.deployment import _candidate_ids
    return _candidate_ids(docker, "conix-" + name)


def prepare(decl, docker, images):
    """Build only from present trusted inputs; stage a retained private volume.

    Preparation may run on the rootful CI Engine, like the existing offline
    build/staging lanes. Actual service start still requires rootless admission.
    """
    from tools.wheelhouse.operations import SharedWheelhouse

    with _locked(decl.name):
        if _inspect_existing(docker, decl.name):
            raise OperationError("Retire the existing service before preparing replacement inputs.")
        existing = None
        if (_directory(decl.name) / PREPARED).exists():
            existing = _prepared(decl)

        pool = SharedWheelhouse(docker, images, decl.wheelhouse)
        pool.check()
        image = pool.build("indexer")
        docker.check_indexer(image)
        volume = docker.stage_inputs(
            image, decl.source_config, decl.runtime_dsn_file, announce=False,
        )
        try:
            _write_prepared(_directory(decl.name), {
                "version": 1, "image": image, "volume": volume,
            })
        except BaseException:
            try:
                docker.remove_inputs(volume)
            except Exception:
                print(f"Unconfirmed cleanup of private input volume {volume}.", file=sys.stderr)
            raise

        if existing and existing["volume"] != volume:
            try:
                docker.remove_inputs(existing["volume"])
            except OperationError:
                print(f"Old input volume {existing['volume']} remains; retire it explicitly.",
                      file=sys.stderr)
        print(f"Prepared deployment {decl.name} with verified image and private input snapshot.")
        print(f"Next: make deployment/database/init DEPLOYMENT={decl.name}")


def database_init(decl, docker, receipt):
    """Send owner DSN through private stdin only; never grant it to the runtime."""
    docker.require_rootless_engine()
    docker.require_role(receipt["image"], "indexer")
    _admit_volume(docker, receipt["volume"])
    network_id = _admit_network(docker, decl.database_network)
    owner = docker.private_bytes(decl.owner_dsn_file, 8192, secret=True)
    docker.verify_inputs(receipt["image"], receipt["volume"])

    argv = docker.docker_command_prefix + [
        "run", "--rm", "--interactive", "--pull", "never",
        "--network", network_id, "--read-only", "--user", "65532:65532",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--pids-limit", "32", "--memory", "256m", "--memory-swap", "256m",
        "--mount", f"type=volume,source={receipt['volume']},target=/run/conix,readonly",
        "--entrypoint", "python", receipt["image"], "-I", "-m", "conix.schema_cli", "initialize",
    ]
    try:
        result = subprocess.run(argv, input=owner, capture_output=True,
                                cwd=ROOT, env=docker.docker_environment, timeout=90)
    except subprocess.TimeoutExpired as error:
        raise OperationError("Schema initializer timed out; inspect the database before retrying.") from error
    except OSError as error:
        raise OperationError("Could not launch the contained owner-only initializer.") from error
    if result.returncode:
        raise OperationError(
            f"Schema initializer failed (exit {result.returncode}); "
            "inspect the owner role, selected database and durable schema before retrying."
        )
    print(f"Initialized fresh Conix history for deployment {decl.name}.")
    print(f"Next: make deployment/up DEPLOYMENT={decl.name}")


def dispatch(action, options, *, client_cls, pinned_images):
    """Use saved selections for all repeat operations; no implicit acquisition."""
    if action == "test/standalone/run":
        if options.origin != "command line" or not CONTEXT_NAME.fullmatch(options.context or ""):
            raise OperationError("Select DOCKER_CONTEXT=<local-rootless-context> for the standalone test.")
        if not options.wheelhouse:
            raise OperationError("Select WHEELHOUSE=<existing-shared-pool> for the standalone test.")
        from tools.wheelhouse.operations import SharedWheelhouse

        endpoint = select_endpoint(options.context, "command line", working_dir=ROOT)
        with tempfile.TemporaryDirectory(prefix="conix-standalone-operator-") as folder:
            config = Path(folder)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}\n')
            docker = client_cls(endpoint, config)
            images = pinned_images()
            pool = SharedWheelhouse(docker, images, options.wheelhouse)
            pool.check()
            runner = pool.build("test-runner")
            docker.test(runner, images)
        return

    name = options.deployment
    _directory(name)
    if action == "deployment/init":
        return init(name, options)

    decl = None
    try:
        decl = _read(name)
    except OperationError:
        if action not in ("deployment/status", "deployment/logs", "deployment/down"):
            raise
        # Only a MISSING declaration permits explicit recovery; corrupted or
        # foreign-owned declarations must not silently redirect operation.
        declaration_path = _directory(name) / "deployment.toml"
        if declaration_path.exists() or declaration_path.is_symlink():
            raise
        if options.origin != "command line" or not CONTEXT_NAME.fullmatch(options.context or ""):
            raise
    if decl:
        if options.context and options.origin == "command line" and options.context != decl.docker_context:
            raise OperationError("The selected Docker context differs from the saved deployment.")
        context = decl.docker_context
    else:
        context = options.context

    if action == "deployment/prepare":
        docker, directory = _docker(decl, client_cls)
        try:
            prepare(decl, docker, pinned_images())
        finally:
            directory.cleanup()
        return

    if action == "deployment/config/check":
        receipt = _prepared(decl)
        docker, directory = _docker(decl, client_cls)
        try:
            docker.deployment(
                operation="config/check", name=name, image=receipt["image"],
                volume=receipt["volume"], database=decl.database_network,
                rpc=decl.rpc_network,
            )
        finally:
            directory.cleanup()
        return

    if action in ("deployment/database/init", "deployment/up"):
        docker, directory = _docker(decl, client_cls)
        try:
            with _locked(name):
                # Prepare and activate use the same deployment lock. Read the
                # receipt under it so a newer input volume cannot replace the
                # selected snapshot before the service is created.
                receipt = _prepared(decl)
                if action == "deployment/database/init":
                    if _inspect_existing(docker, name):
                        raise OperationError("Stop the selected deployment before schema initialization.")
                    database_init(decl, docker, receipt)
                else:
                    docker.deployment(
                        operation="start", name=name, image=receipt["image"],
                        volume=receipt["volume"], database=decl.database_network,
                        rpc=decl.rpc_network,
                    )
        finally:
            directory.cleanup()
        return

    if action in ("deployment/status", "deployment/logs", "deployment/down"):
        # In recovery, the operator must still explicitly choose an Engine.
        endpoint = select_endpoint(context, "command line", working_dir=ROOT)
        with tempfile.TemporaryDirectory(prefix="conix-recovery-client-") as directory:
            (Path(directory) / "config.json").write_text('{"auths":{},"proxies":{}}\n')
            docker = client_cls(endpoint, Path(directory))
            operation = {
                "deployment/status": "status",
                "deployment/logs": "logs",
                "deployment/down": "remove",
            }[action]
            if action == "deployment/down" and decl is not None:
                with _locked(name):
                    return docker.deployment(operation=operation, name=name, image=None,
                                             volume=None, database=None, rpc=None)
            return docker.deployment(operation=operation, name=name, image=None,
                                     volume=None, database=None, rpc=None)

    raise OperationError("Unsupported named deployment command.")
