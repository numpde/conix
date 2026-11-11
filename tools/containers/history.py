"""Run one installed history advance on an explicitly selected Engine."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import time

from tools.containers.client import CONTEXT_NAME, OperationError


ENGINE_ID = re.compile(r"[a-f0-9]{64}\Z")
VOLUME_NAME = re.compile(r"conix-inputs-([a-f0-9]{24})\Z")
RUN_LABEL = "io.conix.history-run"
COMMAND = ["-I", "-m", "conix.worker", "advance"]
OUTPUT_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class WorkerInvocation:
    """A bounded installed-Python entry point, not an arbitrary host command."""

    module: str
    arguments: tuple[str, ...]
    timeout_seconds: int = 330

    def __post_init__(self):
        if (
            type(self.module) is not str
            or re.fullmatch(r"conix(?:\.[a-z][a-z0-9_]*){1,4}", self.module) is None
            or type(self.arguments) is not tuple
            or len(self.arguments) > 6
            or any(type(arg) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", arg) is None
                   for arg in self.arguments)
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 3600
        ):
            raise OperationError("Select a bounded installed Conix worker entry point.")

    @property
    def command(self):
        return ["-I", "-m", self.module, *self.arguments]


BASE_WORKER = WorkerInvocation("conix.worker", ("advance",))


def _record(docker, kind, identity):
    try:
        record = json.loads(docker.run(kind, "inspect", "--format", "{{json .}}", identity,
                                      capture=True, timeout=10))
    except ValueError as error:
        raise OperationError(f"Cannot read the selected Docker {kind} record.") from error
    if type(record) is not dict:
        raise OperationError(f"Docker returned an invalid {kind} record.")
    return record


def _admit_volume(docker, volume):
    if not (match := VOLUME_NAME.fullmatch(volume or "")):
        raise OperationError("Select an exact Conix private input volume.")
    record = _record(docker, "volume", volume)
    if (record.get("Name") != volume or record.get("Driver") != "local"
            or record.get("Scope") != "local" or record.get("Options")
            or record.get("Labels") != {"io.conix.input-token": match[1]}):
        raise OperationError("The selected volume is not a local Conix input snapshot.")


def _admit_network(docker, name):
    if not CONTEXT_NAME.fullmatch(name or ""):
        raise OperationError("Select each existing database and RPC network by exact name.")
    record = _record(docker, "network", name)
    network_id = record.get("Id")
    if (record.get("Name") != name or record.get("Internal") is not True
            or record.get("Driver") != "bridge" or record.get("Scope") != "local"
            or type(network_id) is not str or not ENGINE_ID.fullmatch(network_id)):
        raise OperationError("The selected network must be an internal local Docker bridge.")
    return network_id


def _owned(record, container_id, name, image, token):
    try:
        return (
            record["Id"] == container_id and record["Name"] == "/" + name
            and record["Image"] == image and record["Config"]["Labels"][RUN_LABEL] == token
        )
    except (KeyError, TypeError):
        return False


def _admit_container(record, volume, networks, invocation=BASE_WORKER):
    try:
        config = record["Config"]
        host = record["HostConfig"]
        (mount,) = record["Mounts"]
        attached = record["NetworkSettings"]["Networks"]
        confined = (
            record["State"]["Status"] == "created"
            and config["User"] == "65532:65532"
            and config["Entrypoint"] == ["python"] and config["Cmd"] == invocation.command
            and config["StopSignal"] == "SIGINT" and host["Init"] is True
            and host["ReadonlyRootfs"] is True and host["Privileged"] is False
            and host["CapDrop"] == ["ALL"] and not host["CapAdd"]
            and host["SecurityOpt"] == ["no-new-privileges:true"]
            and host["Memory"] == host["MemorySwap"] == 128 * 1024 * 1024
            and host["PidsLimit"] == 32 and host["NanoCpus"] == 10**9
            and host["LogConfig"] == {"Type": "local", "Config": {"max-size": "32k", "max-file": "1", "compress": "false"}}
            and not any(host.get(key) for key in ("Binds", "PortBindings", "Devices", "DeviceRequests", "PidMode"))
            and host["PublishAllPorts"] is False
            and mount["Type"] == "volume" and mount["Name"] == volume
            and mount["Destination"] == "/run/conix" and mount["RW"] is False
            and _selected_networks(attached, networks)
        )
    except (ValueError, KeyError, TypeError) as error:
        raise OperationError("Docker omitted facts required to admit the stopped history worker.") from error
    if not confined:
        raise OperationError("The stopped history worker does not have the selected confinement grants.")


def _selected_networks(attached, networks):
    if type(attached) is not dict or len(attached) != 2:
        return False
    keys = set(attached)
    for (name, network_id) in networks:
        matching = keys.intersection((name, network_id))
        if len(matching) != 1:
            return False
        (key,) = matching
        endpoint = attached[key]
        # On the selected Engine a stopped container has configured keys but
        # acquires endpoint network IDs only when it starts.
        if type(endpoint) is not dict or endpoint.get("NetworkID") not in ("", network_id):
            return False
        keys.remove(key)
    return not keys


def _candidates(docker, filter_):
    output = docker.run(
        "container", "ls", "--all", "--no-trunc", "--filter", filter_,
        "--format", "{{.ID}}", capture=True, timeout=10,
    )
    candidates = output.splitlines()
    if len(candidates) > 1 or any(not ENGINE_ID.fullmatch(candidate) for candidate in candidates):
        raise OperationError("Cannot establish one exact history worker identity.")
    return candidates


def _retire(docker, token, name, image, known_id):
    label_filter = "label=" + RUN_LABEL + "=" + token
    candidates = _candidates(docker, label_filter)
    if known_id:
        exact = _candidates(docker, "id=" + known_id)
        if exact and exact != candidates:
            raise OperationError("The selected history worker ownership label changed.")
    if candidates:
        (container_id,) = candidates
        record = _record(docker, "container", container_id)
        if not _owned(record, container_id, name, image, token):
            raise OperationError("The selected history worker ownership facts changed.")
        state = record.get("State")
        if type(state) is not dict or type(state.get("Running")) is not bool:
            raise OperationError("Cannot establish whether the owned history worker is running.")
        if state["Running"]:
            try:
                docker.run("container", "stop", "--time", "5", container_id, capture=True, timeout=15)
            except OperationError:
                pass  # The exact container still needs retirement.
        docker.run("container", "rm", "--force", container_id, capture=True, timeout=20)
        known_id = container_id
    if _candidates(docker, label_filter) or (known_id and _candidates(docker, "id=" + known_id)):
        raise OperationError("The owned history worker is still present.")


def _attached(docker, container_id, cancelled=lambda: False, *, timeout_seconds=330):
    """Capture one worker result with a byte bound and a host deadline."""
    command = docker.docker_command_prefix + ["container", "start", "--attach", container_id]
    started = time.monotonic()
    deadline = started + timeout_seconds
    next_report = started + 15
    child = subprocess.Popen(
        command, cwd=docker.working_dir, env=docker.docker_environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    stdout = bytearray()
    stderr = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, stdout)
            selector.register(child.stderr, selectors.EVENT_READ, stderr)
            while selector.get_map():
                if cancelled():
                    raise KeyboardInterrupt
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError
                if now >= next_report:
                    print(f"[conix] History advance: running ({int(now - started)}s)", file=sys.stderr, flush=True)
                    next_report = now + 15
                for (key, _) in selector.select(min(deadline, next_report, now + 1) - now):
                    chunk = os.read(key.fd, OUTPUT_LIMIT + 1)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk)
                    if len(key.data) > OUTPUT_LIMIT:
                        raise OperationError("The history worker exceeded its output bound. Inspect durable progress.")
        return (child.wait(timeout=min(5, max(deadline - time.monotonic(), 0))), bytes(stdout), bytes(stderr))
    finally:
        primary = sys.exception()
        try:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup = "Cannot confirm retirement of the history attach client."
            if primary is None:
                raise OperationError(cleanup) from error
            _notice(cleanup)
        finally:
            child.stdout.close()
            child.stderr.close()


def _notice(message):
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass  # Failure to report a secondary outcome cannot erase the primary one.


def _unique_fields(fields):
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("Duplicate history receipt field.")
    return result


def _receipt(stdout):
    try:
        result = json.loads(stdout, object_pairs_hook=_unique_fields)
        if type(result) is not dict or not ENGINE_ID.fullmatch(result["source_id"]):
            raise ValueError
        if result["outcome"] == "at_tip" and set(result) == {"outcome", "source_id"}:
            return result
        if (result["outcome"] == "advanced"
                and set(result) == {"outcome", "source_id", "block_number", "block_hash", "revision"}
                and type(result["block_number"]) is int and 0 <= result["block_number"] < 2**63
                and type(result["revision"]) is int and 1 <= result["revision"] < 2**63
                and re.fullmatch(r"0x[a-f0-9]{64}", result["block_hash"])):
            return result
    except (ValueError, KeyError, TypeError) as error:
        raise OperationError("Cannot confirm the history worker receipt. Inspect durable progress.") from error
    raise OperationError("Cannot confirm the history worker receipt. Inspect durable progress.")


_SAFE_WORKER_CAUSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,:;()/'=+-]{0,239}\Z")
_WORKER_FAILURE = re.compile(
    rb"(Cannot load staged inputs|Index advance (?:failed|was interrupted)) "
    rb"\((InputError|BranchConflict|HistoryConflict|StorageUnavailable|WriterBusy|"
    rb"RpcProtocolError|RpcUnavailable|MalformedABIData|ResourceExceeded|"
    rb"TimeoutError|KeyboardInterrupt)\)\. ([^\r\n]{1,240})\n?\Z"
)


def _admitted_worker_cause(value):
    """Filter bounded cause text from the installed worker's known failures.

    Character and keyword checks do not establish the text's provenance.
    The installed worker owns these messages and omits raw RPC and SQL prose.
    """
    return (
        _SAFE_WORKER_CAUSE.fullmatch(value) is not None
        and not any(marker in value.lower() for marker in (
            "private", "password", "secret", "token", "dsn", "://",
        ))
        and re.search(r"(?:0x)?[0-9a-fA-F]{64}", value) is None
    )


def _failure(stderr, exit_code):
    """Preserve classified worker causes without trusting arbitrary stderr."""
    if stderr.startswith(b"Index advance has an unconfirmed PostgreSQL COMMIT."):
        return OperationError(
            f"Index advance has an unconfirmed PostgreSQL COMMIT (worker exit {exit_code}). "
            "Inspect durable progress before another advance."
        )

    if match := _WORKER_FAILURE.fullmatch(stderr):
        # Known worker failures use ASCII literals and bounded scalar facts.
        # The byte matcher alone does not establish that encoding: a producer
        # that adds non-ASCII causes would need decoding refusal here first.
        (operation, category, cause) = (field.decode("ascii") for field in match.groups())
        if _admitted_worker_cause(cause):
            return OperationError(f"{operation} ({category}; worker exit {exit_code}). {cause}")

    match = re.search(
        rb"\A(?:Cannot load staged inputs|Index advance (?:failed|was interrupted)) "
        rb"\((InputError|BranchConflict|HistoryConflict|StorageUnavailable|WriterBusy|RpcProtocolError|"
        rb"RpcUnavailable|MalformedABIData|ResourceExceeded|TimeoutError|KeyboardInterrupt)\)\.",
        stderr,
    )
    if match:
        category = match[1].decode("ascii")
        return OperationError(
            f"History advance failed ({category}; worker exit {exit_code}). "
            "Inspect durable progress before another run."
        )

    if match := re.fullmatch(
        rb"Index advance failed during (staged inputs|advance) \(([A-Za-z][A-Za-z0-9_]{0,63})\) "
        rb"at ([A-Za-z0-9_]{1,64}\.py:[1-9][0-9]{0,5})\.\n?",
        stderr,
    ):
        (phase, category, location) = (field.decode("ascii") for field in match.groups())
        return OperationError(
            f"History advance failed during {phase} ({category}) at {location}; "
            f"worker exit {exit_code}."
        )

    return OperationError(
        f"History advance exit {exit_code} has no admitted diagnostic. Inspect durable progress."
    )

@contextmanager
def _cancellation_requests():
    requested = False

    def request(_signum, _frame):
        nonlocal requested
        requested = True

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        for signum in previous:
            signal.signal(signum, request)
        yield lambda: requested
    finally:
        for (signum, handler) in previous.items():
            signal.signal(signum, handler)


def advance_history(docker, *, image, volume, database, rpc):
    """Keep the existing one-shot CLI and its exact core receipt unchanged."""
    with _cancellation_requests() as cancelled:
        return _advance_history(docker, image=image, volume=volume, database=database,
                                rpc=rpc, cancelled=cancelled)


def run_history_program(docker, *, image, volume, database, rpc, invocation, parse_receipt):
    """Execute an installed Conix driver within the same confined envelope.

    Only a trusted composition selects the driver and supplies its receipt
    parser. This grants no journal authority or permission to bypass Tier 0.
    The caller must independently qualify that its driver delegates to the
    concrete core and rejects unconfirmed outcomes.
    """
    if type(invocation) is not WorkerInvocation or not callable(parse_receipt):
        raise OperationError("Select an installed worker and a receipt decoder.")

    with _cancellation_requests() as cancelled:
        return _advance_history(docker, image=image, volume=volume, database=database,
                                rpc=rpc, cancelled=cancelled, invocation=invocation,
                                parse_receipt=parse_receipt, emit_receipt=False)


def _advance_history(docker, *, image, volume, database, rpc, cancelled,
                     invocation=BASE_WORKER, parse_receipt=_receipt, emit_receipt=True):
    """Admit the grants, run one worker, and retire only its container."""
    docker.require_rootless_engine()
    docker.require_role(image, "indexer")
    _admit_volume(docker, volume)
    database_id = _admit_network(docker, database)
    rpc_id = _admit_network(docker, rpc)
    if database_id == rpc_id:
        raise OperationError("History advance requires distinct database and RPC networks.")

    token = secrets.token_hex(12)
    name = "conix-advance-" + token
    receipt = None
    failure = None
    known_id = None
    try:
        if cancelled():
            raise KeyboardInterrupt
        container_id = _create(docker, image, volume, (database_id, rpc_id), name, token,
                               invocation=invocation)
        if not ENGINE_ID.fullmatch(container_id):
            raise OperationError("Cannot confirm the created history worker identity.")
        known_id = container_id
        record = _record(docker, "container", container_id)
        if not _owned(record, container_id, name, image, token):
            raise OperationError("Cannot confirm ownership of the created history worker.")
        _admit_container(record, volume, ((database, database_id), (rpc, rpc_id)), invocation)
        # Creation attaches the named volume. Check the mounted identity
        # again before starting in case Docker replaced a missing name.
        _admit_volume(docker, volume)
        if cancelled():
            raise KeyboardInterrupt

        print(f"[conix] History advance {name}: started", file=sys.stderr, flush=True)
        (client_exit, stdout, stderr) = _attached(
            docker, container_id, cancelled, timeout_seconds=invocation.timeout_seconds,
        )
        if stdout:
            try:
                receipt = parse_receipt(stdout)
            except OperationError:
                pass  # Worker diagnostics remain primary when stdout is not a receipt.
        exit_status = docker.run("container", "wait", container_id, capture=True, timeout=15).strip()
        if not re.fullmatch(r"0|[1-9][0-9]{0,2}", exit_status) or int(exit_status) > 255:
            raise OperationError("Cannot confirm the history worker exit. Inspect durable progress.")
        exit_code = int(exit_status)
        if client_exit:
            state = _record(docker, "container", container_id).get("State")
            if type(state) is dict and state.get("Status") == "created" and state.get("Error"):
                raise OperationError("Docker rejected the history worker start before advance began.")
        if exit_code:
            if invocation == BASE_WORKER:
                raise _failure(stderr, exit_code)
            raise OperationError(
                f"Selected worker failed (exit {exit_code}). Inspect durable progress."
            )
        if receipt is None:
            receipt = parse_receipt(stdout)
        if client_exit or stderr:
            raise OperationError(f"The attach client reported a failure after worker completion (exit {client_exit}).")
        if cancelled():
            raise KeyboardInterrupt
    except (KeyboardInterrupt, TimeoutError, subprocess.TimeoutExpired) as error:
        failure = OperationError(
            f"History advance {name} did not confirm completion ({type(error).__name__}). Inspect durable progress."
        )
    except OperationError as error:
        failure = error
    except OSError as error:
        failure = OperationError(f"History worker communication failed (errno={error.errno}). Inspect durable progress.")
    finally:
        try:
            _retire(docker, token, name, image, known_id)
        except OperationError as error:
            cleanup = OperationError(f"Cannot confirm retirement of {name} on {docker.endpoint}. {error}")
            if failure is None:
                failure = cleanup
            else:
                _notice(cleanup)
        if cancelled() and failure is None:
            failure = OperationError("History advance was interrupted during worker retirement.")

    if failure is not None:
        if receipt is not None:
            _notice(f"Worker outcome before the operator failure: {json.dumps(receipt)}")
        raise failure
    if emit_receipt:
        try:
            print(json.dumps(receipt), flush=True)
        except OSError as error:
            _notice(f"Worker completed and retired: {json.dumps(receipt)}")
            raise OperationError("Cannot deliver the history advance receipt on stdout.") from error
    _notice("[conix] History advance: completed and retired")
    return receipt


def _create(docker, image, volume, networks, name, token, *, invocation=BASE_WORKER):
    (database_id, rpc_id) = networks
    try:
        return docker.run(
            "container", "create", "--name", name, "--label", RUN_LABEL + "=" + token,
            "--pull", "never", "--init", "--stop-signal", "SIGINT", "--read-only",
            "--user", "65532:65532", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "128m", "--memory-swap", "128m", "--cpus", "1",
            "--network", database_id, "--network", rpc_id,
            "--mount", f"type=volume,source={volume},target=/run/conix,readonly,volume-nocopy",
            "--log-driver", "local", "--log-opt", "max-size=32k", "--log-opt", "max-file=1",
            "--log-opt", "compress=false",
            "--entrypoint", "python", image, *invocation.command, capture=True, timeout=30,
        ).strip()
    except OperationError as error:
        raise OperationError(f"Cannot confirm creation of {name} on {docker.endpoint}. {error}") from error
