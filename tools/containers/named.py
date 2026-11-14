"""Named Conix deployments: one editable definition, one generated TOML lock.

The host operator projects the definition into the installed Tier-0 catalog.
Only the projected catalog, its selected ABIs and runtime DSN are staged.
The separately owned schema credential is read only by database/schema/init.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib

from tools.containers.client import CONTEXT_NAME, IMAGE_ID, OperationError, select_endpoint
from tools.containers.resources import VOLUME_NAME, _admit_network, _admit_volume


ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENTS = ROOT / "config" / "deployments"
NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
KEY = re.compile(r"[a-z][a-z0-9_]*\Z")
LOCK_FILE = "deployment.lock.toml"
READER_LOCK_FILE = "reader.lock.toml"
SETTINGS = {"version", "docker_context", "wheelhouse", "database_network",
            "rpc_network", "chain", "contracts", "limits"}


@dataclass(frozen=True)
class Declaration:
    name: str
    docker_context: str
    wheelhouse: str = ""
    database_network: str = ""
    rpc_network: str = ""
    source: dict | None = None

    @property
    def folder(self):
        return _directory(self.name)

    @property
    def abi_dir(self):
        return self.folder / "abi"

    @property
    def runtime_dsn_file(self):
        return self.folder / "secrets" / "runtime.dsn"

    @property
    def owner_dsn_file(self):
        return self.folder / "secrets" / "owner.dsn"

    @property
    def lock_file(self):
        return self.folder / LOCK_FILE

    @property
    def reader_dsn_file(self):
        return self.folder / "secrets" / "reader.dsn"

    @property
    def reader_lock_file(self):
        return self.folder / READER_LOCK_FILE


@dataclass(frozen=True)
class DeploymentView:
    """Public read-only deployment facts needed by the acceptance consumer."""
    name: str
    docker_context: str
    wheelhouse: str
    database_network: str
    rpc_network: str
    runtime_dsn_file: Path
    genesis_hash: str


@dataclass(frozen=True)
class PreparedView:
    """Exact image/volume identities without exposing lock representation."""
    image: str
    volume: str


def inspect_named(name):
    """Inspect only the selected source/Engine coordinates without Docker."""
    declaration = _read(name)
    return DeploymentView(
        name=name,
        docker_context=declaration.docker_context,
        wheelhouse=declaration.wheelhouse,
        database_network=declaration.database_network,
        rpc_network=declaration.rpc_network,
        runtime_dsn_file=declaration.runtime_dsn_file,
        genesis_hash=declaration.source["chain"]["genesis_hash"],
    )


def inspect_prepared(name):
    """Inspect one admitted prepared selection; absence is not a valid lock."""
    folder = _directory(name)
    _owned_directory(folder)
    lock = folder / LOCK_FILE
    if not lock.exists() and not lock.is_symlink():
        return None
    record = _lock(Declaration(name=name, docker_context=""))
    return PreparedView(image=record["image"], volume=record["volume"])


def inspect_reader_prepared(name):
    """Expose only the Reader's admitted image/volume for acceptance consumers."""
    folder = _directory(name)
    _owned_directory(folder)
    lock = folder / READER_LOCK_FILE
    if not lock.exists() and not lock.is_symlink():
        return None
    receipt = _reader_lock(Declaration(name=name, docker_context=""))
    return PreparedView(image=receipt["image"], volume=receipt["volume"])


def clear_prepared(name, *, expected_volume):
    """Forget a prepared selection only after its exact volume was retired.

    The acceptance owner performs its own Docker removal first; this function
    cannot acquire resource-retirement authority or delete an unrelated lock.
    """
    with _locked(name):
        prepared = inspect_prepared(name)
        if prepared is None or prepared.volume != expected_volume:
            raise OperationError("Prepared input selection changed; retaining its lock.")
        (_directory(name) / LOCK_FILE).unlink()


def _directory(name):
    if type(name) is not str or NAME.fullmatch(name) is None:
        raise OperationError("Select DEPLOYMENT=<lowercase-name> without path separators.")
    return DEPLOYMENTS / name


def _owned_directory(path):
    try:
        details = path.lstat()
    except OSError as error:
        raise OperationError(f"Deployment directory {path.name!r} is unavailable.") from error
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise OperationError(f"Deployment directory {path.name!r} must be owner-only (0700).")


def _private_file(path):
    try:
        info = path.lstat()
    except OSError as error:
        raise OperationError(f"Required deployment file {path.name!r} is unavailable.") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise OperationError(f"Deployment file {path.name!r} must be an owner-only regular file (0600).")
    return path


def _read_toml(path, max_bytes):
    _private_file(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise OperationError("Selected deployment file changed during inspection.")
            data = stream.read(max_bytes + 1)
    except OSError as error:
        raise OperationError(f"Cannot read deployment file {path.name!r}.") from error
    if not 0 < len(data) <= max_bytes:
        raise OperationError(f"Deployment file {path.name!r} is empty or exceeds its size limit.")
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise OperationError(f"Deployment file {path.name!r} is not valid UTF-8 TOML.") from error


def _scalar(value):
    """Serialize only TOML values required by the existing Tier-0 catalog."""
    if type(value) is str:
        return json.dumps(value, ensure_ascii=True)
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    if type(value) is list:
        return "[" + ", ".join(_scalar(part) for part in value) + "]"
    raise OperationError("The selected source contains a value that cannot be projected as TOML.")


def _table(label, data):
    if type(data) is not dict:
        raise OperationError(f"Deployment {label} must be a TOML table.")
    rows = [f"[{label}]\n"]
    for name, value in data.items():
        if type(name) is not str or KEY.fullmatch(name) is None:
            raise OperationError(f"Deployment {label} contains an invalid field name.")
        rows.append(f"{name} = {_scalar(value)}\n")
    return "".join(rows) + "\n"


def _render_deployment(document):
    """Render editable deployment data without inventing any source settings."""
    keys = ("version", "docker_context", "wheelhouse", "database_network", "rpc_network")
    output = []
    for key in keys:
        if key in document:
            output.append(f"{key} = {_scalar(document[key])}\n")
    output.append("\n")
    for name in ("chain", "limits"):
        if name in document:
            output.append(_table(name, document[name]))
    contracts = document.get("contracts", {})
    if type(contracts) is not dict:
        raise OperationError("Deployment contracts must be a TOML table.")
    for alias, selected in contracts.items():
        if type(alias) is not str or KEY.fullmatch(alias) is None:
            raise OperationError("Deployment contract alias cannot be rendered.")
        output.append(_table(f"contracts.{alias}", selected))
    return "".join(output)


def _project_catalog(decl, *, reader=False):
    """Project one selected source; only the database ROLE differs for Readers."""
    if decl.source is None:
        raise OperationError("Complete the source settings in deployment.toml before preparation.")
    document = {"version": 1, **decl.source,
                "database": {"dsn_file": "/run/conix/connection.dsn",
                             "runtime_role": "conix_reader" if reader else "conix_runtime"}}
    output = ["version = 1\n\n", _table("chain", document["chain"]),
              _table("database", document["database"]), _table("limits", document["limits"])]
    for alias, selected in document["contracts"].items():
        output.append(_table(f"contracts.{alias}", selected))
    return "".join(output)


def _read(name, *, complete=True):
    folder = _directory(name)
    _owned_directory(folder)
    document = _read_toml(folder / "deployment.toml", 64 * 1024)
    if type(document) is not dict or set(document) - SETTINGS:
        raise OperationError("deployment.toml contains unsupported settings.")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise OperationError("deployment.toml requires version = 1.")
    context = document.get("docker_context")
    if type(context) is not str or CONTEXT_NAME.fullmatch(context) is None:
        raise OperationError("deployment.toml requires a valid docker_context.")

    required = SETTINGS - set(document)
    if complete and required:
        for key in ("wheelhouse", "database_network", "rpc_network", "chain", "contracts", "limits"):
            if key in required:
                raise OperationError(f"deployment.toml is missing required setting: {key}.")
    selections = {}
    for key in ("wheelhouse", "database_network", "rpc_network"):
        value = document.get(key, "")
        if type(value) is not str:
            raise OperationError(f"deployment.toml {key} must be a string.")
        selections[key] = value

    decl = Declaration(
        name=name, docker_context=context, **selections,
        source={key: document[key] for key in ("chain", "contracts", "limits")}
        if complete else None,
    )
    if not complete:
        return decl

    if not Path(decl.wheelhouse).is_absolute() or len(decl.wheelhouse) > 4096:
        raise OperationError("deployment.toml wheelhouse must be an absolute existing pool path.")
    for key in ("database_network", "rpc_network"):
        value = getattr(decl, key)
        if not value or CONTEXT_NAME.fullmatch(value) is None:
            raise OperationError(f"deployment.toml requires a valid {key}.")
    if decl.database_network == decl.rpc_network:
        raise OperationError("Database and RPC network selections must be distinct.")

    chain = decl.source["chain"]
    limits = decl.source["limits"]
    contracts = decl.source["contracts"]
    if type(chain) is not dict or type(limits) is not dict or type(contracts) is not dict:
        raise OperationError("Chain, contracts and limits must be TOML tables.")
    if not contracts:
        raise OperationError("deployment.toml requires at least one selected contract.")
    for alias, selected in contracts.items():
        if (type(alias) is not str or KEY.fullmatch(alias) is None
                or type(selected) is not dict
                or set(selected) != {"address", "abi_file", "events"}):
            raise OperationError("A selected contract requires only address, abi_file and events.")

    # This path projection is a transport constraint; semantic ABI/event/chain
    # admission remains exclusively in the installed Tier-0 catalog.
    from tools.containers.input_staging import _declared_abi_names

    selected_paths = _declared_abi_names(_project_catalog(decl).encode("utf-8"))
    _owned_directory(decl.abi_dir)
    _owned_directory(folder / "secrets")
    for filename in selected_paths:
        path = decl.abi_dir / filename
        try:
            info = path.lstat()
        except OSError as error:
            raise OperationError(f"Selected ABI file {filename!r} is missing.") from error
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise OperationError(f"Selected ABI file {filename!r} must be an owned regular file.")
    return decl


def _write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def init(name, options):
    """Create an editable skeleton and private directories; no source guesses."""
    folder = _directory(name)
    if os.geteuid() == 0:
        raise OperationError("Initialize deployments as the unprivileged Docker operator.")
    if options.origin != "command line" or not CONTEXT_NAME.fullmatch(options.context or ""):
        raise OperationError("deployment/config/init requires DOCKER_CONTEXT=<local-context> explicitly.")
    if folder.exists() or folder.is_symlink():
        raise OperationError("Named deployment already exists; initialization will not overwrite it.")
    for path in (ROOT / "config", DEPLOYMENTS):
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
                raise OperationError("Deployment configuration parent is not an owned directory.")
        else:
            path.mkdir(mode=0o700)
    folder.mkdir(mode=0o700)
    (folder / "abi").mkdir(mode=0o700)
    (folder / "secrets").mkdir(mode=0o700)
    skeleton = (
        "version = 1\n"
        + "docker_context = " + _scalar(options.context) + "\n\n"
        + "# Fill in all required settings before deployment/prepare.\n"
        + "# wheelhouse = \"/absolute/path/to/wheelhouse\"\n"
        + "# database_network = \"<existing database bridge>\"\n"
        + "# rpc_network = \"<existing RPC bridge>\"\n\n"
        + "# [chain]           # supply chain_id, genesis_hash, start_block, rpc_url\n"
        + "# [limits]          # supply all Tier-0 limits (see examples/conix.toml)\n"
        + "# [contracts.token] # supply address, abi_file = \"abi/token.json\", events\n"
    )
    _write_new(folder / "deployment.toml", skeleton.encode("utf-8"))
    print(f"Initialized deployment {name}; fill deployment.toml and install selected ABIs/DSNs.")
    print(f"Next: make deployment/prepare DEPLOYMENT={name}")


@contextmanager
def _locked(name):
    """Serialize named mutations by locking their existing private directory."""
    folder = _directory(name)
    _owned_directory(folder)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OperationError("Another operation owns this named deployment.") from error
        yield
    finally:
        os.close(fd)


def _valid_lock(record):
    if (type(record) is not dict or set(record) != {"version", "image", "volume"}
            or type(record["version"]) is not int or record["version"] != 1
            or type(record["image"]) is not str or IMAGE_ID.fullmatch(record["image"]) is None
            or type(record["volume"]) is not str or VOLUME_NAME.fullmatch(record["volume"]) is None):
        raise OperationError("deployment.lock.toml requires exact version, image and volume identities.")
    return record


def _lock(decl, *, reader=False):
    """Read the selected role's generated TOML lock; never guess resources."""
    filename = READER_LOCK_FILE if reader else LOCK_FILE
    path = decl.folder / filename
    try:
        document = _read_toml(path, 4096)
    except OperationError as error:
        action = "deployment/reader/prepare" if reader else "deployment/prepare"
        raise OperationError(
            f"No valid {filename} for {decl.name}; "
            f"run make {action} DEPLOYMENT={decl.name}."
        ) from error
    return _valid_lock(document)


def _reader_lock(decl):
    """Refuse a Reader receipt that selects the writer's owned input volume."""
    receipt = _lock(decl, reader=True)
    if decl.lock_file.exists() or decl.lock_file.is_symlink():
        writer = _lock(decl)
        if receipt["volume"] == writer["volume"]:
            raise OperationError("Reader and indexer input volumes must be distinct.")
    return receipt


def _write_lock(folder, record, *, reader=False):
    """Publish a role-specific 0600 lock after verified snapshot staging."""
    _valid_lock(record)
    destination = folder / (READER_LOCK_FILE if reader else LOCK_FILE)
    if destination.exists() or destination.is_symlink():
        _private_file(destination)
    contents = (
        "version = 1\n"
        f"image = {_scalar(record['image'])}\n"
        f"volume = {_scalar(record['volume'])}\n"
    )
    fd, temporary = tempfile.mkstemp(prefix=".deployment-lock-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(contents.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        # A successful replace is the publication boundary. Never attempt
        # cleanup of the new volume after that boundary: it is now selected.
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _docker(decl, client_cls):
    endpoint = select_endpoint(decl.docker_context, "command line", working_dir=ROOT)
    config_dir = tempfile.TemporaryDirectory(prefix="conix-named-docker-")
    (Path(config_dir.name) / "config.json").write_text('{"auths":{},"proxies":{}}\n')
    return (client_cls(endpoint, Path(config_dir.name)), config_dir)


def _inspect_existing(docker, name):
    from tools.containers.deployment import _candidate_ids
    return _candidate_ids(docker, "conix-" + name)


def _stage_projected(decl, docker, image, *, reader=False):
    """Project one role's catalog beside selected ABIs, retiring temporary source."""
    fd, temporary = tempfile.mkstemp(prefix=".projected-", suffix=".toml", dir=decl.folder)
    volume = None
    failure = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(_project_catalog(decl, reader=reader))
            stream.flush()
            os.fsync(stream.fileno())
        volume = docker.stage_inputs(
            image, temporary,
            decl.reader_dsn_file if reader else decl.runtime_dsn_file,
            announce=False,
        )
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError as error:
            if volume is not None:
                try:
                    docker.remove_inputs(volume)
                except Exception:
                    print(f"Unconfirmed cleanup of private input volume {volume}.", file=sys.stderr)
            if failure is not None:
                print("Temporary projected catalog cleanup is unconfirmed.", file=sys.stderr)
            else:
                raise OperationError(
                    "Cannot retire temporary projected catalog; prepared state was not published."
                ) from error
    return volume


def prepare(decl, docker, images):
    """Offline build and stage; publish the TOML lock only after all succeed."""
    from tools.wheelhouse.operations import SharedWheelhouse

    with _locked(decl.name):
        if _inspect_existing(docker, decl.name):
            raise OperationError("Stop the selected service before preparing replacement inputs.")
        existing = _lock(decl) if decl.lock_file.exists() or decl.lock_file.is_symlink() else None
        pool = SharedWheelhouse(docker, images, decl.wheelhouse)
        pool.check()
        image = pool.build("indexer")
        docker.check_indexer(image)
        volume = _stage_projected(decl, docker, image)
        try:
            _write_lock(decl.folder, {"version": 1, "image": image, "volume": volume})
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
                print(f"Previous input volume {existing['volume']} remains; retire explicitly.",
                      file=sys.stderr)
        print(f"Prepared deployment {decl.name} with verified image and private input snapshot.")
        print(f"Next: make deployment/database/schema/init DEPLOYMENT={decl.name}")


def prepare_reader(decl, docker):
    """Stage independent read-only inputs using the indexer's selected image."""
    with _locked(decl.name):
        # Writer preparation remains the sole image/build authority; the
        # reader only reuses the exact installed package, not its DSN/volume.
        prepared = _lock(decl)
        _private_file(decl.reader_dsn_file)
        reader_lock = decl.reader_lock_file
        existing = _reader_lock(decl) if reader_lock.exists() or reader_lock.is_symlink() else None

        docker.require_role(prepared["image"], "indexer")
        volume = _stage_projected(decl, docker, prepared["image"], reader=True)
        try:
            _write_lock(
                decl.folder,
                {"version": 1, "image": prepared["image"], "volume": volume},
                reader=True,
            )
        except BaseException:
            try:
                docker.remove_inputs(volume)
            except Exception:
                print(f"Unconfirmed cleanup of Reader input volume {volume}.", file=sys.stderr)
            raise

        if existing and existing["volume"] != volume:
            try:
                docker.remove_inputs(existing["volume"])
            except OperationError:
                print(f"Prior Reader volume {existing['volume']} remains; retire explicitly.",
                      file=sys.stderr)
        print(f"Prepared Reader for deployment {decl.name} with separate selected inputs.")
        print(f"Next: make deployment/reader/check DEPLOYMENT={decl.name}")


def reader_status(name):
    """Report exact prepared Reader identities without accessing credentials."""
    receipt = _reader_lock(Declaration(name=name, docker_context=""))
    print(json.dumps({
        "deployment": name,
        "image": receipt["image"],
        "volume": receipt["volume"],
        "mount": "/run/conix",
    }, sort_keys=True))


def reader_remove(name, docker):
    """Retire only the Reader's exact volume; preserve its lock on refusal."""
    with _locked(name):
        receipt = _reader_lock(Declaration(name=name, docker_context=""))
        # Recovery must work on the same selected Engine used for preparation,
        # even when its daemon is not eligible to start a Reader process.
        docker.remove_inputs(receipt["volume"])
        (_directory(name) / READER_LOCK_FILE).unlink()
        print(f"Retired Reader input snapshot for deployment {name}; indexer unchanged.")


def database_init(decl, docker, receipt):
    """Send owner DSN only through stdin to a short-lived schema initializer."""
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
        result = subprocess.run(
            argv, input=owner, capture_output=True, cwd=ROOT,
            env=docker.docker_environment, timeout=90,
        )
    except subprocess.TimeoutExpired as error:
        raise OperationError("Schema initializer timed out; inspect database before retrying.") from error
    except OSError as error:
        raise OperationError("Could not launch the contained owner-only initializer.") from error
    if result.returncode:
        raise OperationError(
            f"Schema initializer failed (exit {result.returncode}); "
            "inspect the selected database before retrying."
        )
    print(f"Initialized fresh Conix history for deployment {decl.name}.")
    print(f"Next: make deployment/up DEPLOYMENT={decl.name}")


def dispatch(action, options, *, client_cls, pinned_images):
    """Resolve one named declaration and reuse the existing Docker operator."""
    # Named operations are resolved from one editable file. An explicit
    # obsolete selector must not be silently ignored or override its source.
    old_selectors = {
        "CONFIG_FILE": "config_file",
        "DSN_FILE": "dsn_file",
        "OWNER_DSN_FILE": "owner_dsn_file",
        "INDEXER_IMAGE": "indexer_image",
        "INPUT_VOLUME": "volume",
        "DATABASE_NETWORK": "database_network",
        "RPC_NETWORK": "rpc_network",
        "WHEELHOUSE": "wheelhouse",
    }
    for label, attribute in old_selectors.items():
        if getattr(options, attribute, None):
            raise OperationError(
                f"Named deployment selections belong in deployment.toml; "
                f"remove the {label} override."
            )

    name = options.deployment
    _directory(name)
    if action == "deployment/config/init":
        return init(name, options)

    recovery_actions = (
        "deployment/status", "deployment/logs", "deployment/down",
        "deployment/reader/status", "deployment/reader/remove",
    )
    decl = None
    try:
        decl = _read(name, complete=action not in recovery_actions)
    except OperationError:
        if action not in recovery_actions:
            raise
        # Only absent saved configuration permits explicit-context recovery.
        config = _directory(name) / "deployment.toml"
        if config.exists() or config.is_symlink():
            raise
        if options.origin != "command line" or not CONTEXT_NAME.fullmatch(options.context or ""):
            raise
    if decl:
        if options.context and options.origin == "command line" and options.context != decl.docker_context:
            raise OperationError("The selected Docker context differs from the saved deployment.")
        context = decl.docker_context
    else:
        context = options.context

    if action == "deployment/reader/status":
        return reader_status(name)

    if action in ("deployment/reader/prepare", "deployment/reader/check",
                  "deployment/reader/remove"):
        if action == "deployment/reader/prepare":
            # Before selecting any Engine, require both the writer image
            # selection and a privately owned distinct Reader credential.
            _lock(decl)
            _private_file(decl.reader_dsn_file)
        if action in ("deployment/reader/check", "deployment/reader/remove"):
            _reader_lock(decl or Declaration(name=name, docker_context=context))

        if decl is None:
            endpoint = select_endpoint(context, "command line", working_dir=ROOT)
            directory = tempfile.TemporaryDirectory(prefix="conix-reader-recovery-")
            (Path(directory.name) / "config.json").write_text('{"auths":{},"proxies":{}}\n')
            docker = client_cls(endpoint, Path(directory.name))
        else:
            docker, directory = _docker(decl, client_cls)

        try:
            if action == "deployment/reader/prepare":
                return prepare_reader(decl, docker)
            if action == "deployment/reader/remove":
                return reader_remove(name, docker)
            with _locked(name):
                receipt = _reader_lock(decl)
                return docker.reader_check(
                    name=name, image=receipt["image"], volume=receipt["volume"],
                    database=decl.database_network, rpc=decl.rpc_network,
                )
        finally:
            directory.cleanup()

    if action == "deployment/prepare":
        docker, directory = _docker(decl, client_cls)
        try:
            prepare(decl, docker, pinned_images())
        finally:
            directory.cleanup()
        return

    if action == "deployment/config/check":
        receipt = _lock(decl)
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

    if action in ("deployment/database/schema/init", "deployment/up"):
        with _locked(name):
            # A missing or malformed generated lock is refused before selecting
            # an Engine. The same lock fences preparation and service startup.
            receipt = _lock(decl)
            docker, directory = _docker(decl, client_cls)
            try:
                if action == "deployment/database/schema/init":
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
                    return docker.deployment(
                        operation=operation, name=name, image=None,
                        volume=None, database=None, rpc=None,
                    )
            return docker.deployment(
                operation=operation, name=name, image=None,
                volume=None, database=None, rpc=None,
            )

    raise OperationError("Unsupported named deployment command.")
