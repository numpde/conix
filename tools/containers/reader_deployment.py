"""One-shot named Reader check on privately selected Docker inputs.

A Reader is an independent library consumer, not a persistent service.
The operator selects the indexer's reviewed image and a separate read-only
catalog/ABI/DSN snapshot. Compose joins existing internal database and RPC
bridges without acquiring or owning either network. Each check owns only its
uniquely identified ephemeral container and retires it after admission.
"""

import json
import os
import re
import secrets
import sys
import subprocess
import tempfile

from tools.containers.diagnostics import PREFIX, explanation
from tools.containers.client import IMAGE_ID, OperationError
from tools.containers.resources import (
    ENGINE_ID, VOLUME_NAME, _admit_network, _admit_volume, _record,
)
from tools.containers import named


TOKEN = re.compile(r"[0-9a-f]{24}\Z")


def admit_graph(graph, *, image, volume, database, rpc, token):
    """Refuse any reader process with broader privileges or input grants."""
    try:
        services = graph["services"]
        networks = graph["networks"]
        volumes = graph["volumes"]
        if set(services) != {"reader"}:
            raise OperationError("Reader check must have exactly one contained service.")
        service = services["reader"]
        if (
            set(networks) != {"database", "rpc"}
            or networks["database"].get("name") != database
            or networks["rpc"].get("name") != rpc
            or networks["database"].get("external") is not True
            or networks["rpc"].get("external") is not True
            or set(volumes) != {"inputs"}
            or volumes["inputs"].get("name") != volume
            or volumes["inputs"].get("external") is not True
            or service.get("image") != image
            or service.get("pull_policy") != "never"
            or service.get("entrypoint") != ["python"]
            or service.get("command") != ["-I", "-m", "conix.reader_cli", "check"]
            or service.get("user") != "65532:65532"
            or service.get("read_only") is not True
            or service.get("init") is not True
            or service.get("restart") != "no"
            or service.get("labels") != {"io.conix.reader-check": token}
            or set(service.get("cap_drop", [])) != {"ALL"}
            or set(service.get("security_opt", [])) != {"no-new-privileges:true"}
            or service.get("cap_add") or service.get("privileged")
            or service.get("ports") or service.get("build")
            or service.get("logging") != {
                "driver": "local",
                "options": {"max-size": "1m", "max-file": "1", "compress": "false"},
            }
            or service.get("environment")
            or service.get("devices") or service.get("network_mode")
            or service.get("pid") or service.get("ipc") or service.get("uts")
            or set(service.get("networks", [])) != {"database", "rpc"}
            or service.get("cpus") != 0.5
            or service.get("pids_limit") != 32
            or str(service.get("mem_limit")) not in ("268435456", "256m")
            or str(service.get("memswap_limit")) not in ("268435456", "256m")
            or type(service.get("tmpfs")) is not list or len(service["tmpfs"]) != 1
            or not service["tmpfs"][0].startswith("/tmp:")
            or "size=16m" not in service["tmpfs"][0].split(",")
        ):
            raise OperationError("Reader check violates private-input and confinement policy.")
        mounts = service["volumes"]
        if (
            len(mounts) != 1
            or mounts[0].get("type") != "volume"
            or mounts[0].get("source") not in ("inputs", volume)
            or mounts[0].get("target") != "/run/conix"
            or mounts[0].get("read_only") is not True
            or mounts[0].get("volume", {}).get("nocopy") is not True
        ):
            raise OperationError("Reader must mount exactly its own read-only input snapshot.")
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as error:
        raise OperationError("Cannot admit the selected Reader check configuration.") from error


def _check_candidates(docker, *, project, image, token):
    """Inspect every matching container before retiring any one of them."""
    ids = docker.run(
        "ps", "--all", "--no-trunc", "--filter",
        "label=com.docker.compose.project=" + project,
        "--format", "{{.ID}}", capture=True, timeout=15,
    ).splitlines()
    for identity in ids:
        if ENGINE_ID.fullmatch(identity) is None:
            raise OperationError("Reader check returned an incomplete owned-container identity.")
        record = _record(docker, "container", identity)
        labels = record.get("Config", {}).get("Labels", {})
        if (
            record.get("Id") != identity
            or record.get("Config", {}).get("Image") != image
            or type(labels) is not dict
            or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") != "reader"
            or labels.get("io.conix.reader-check") != token
        ):
            raise OperationError("Reader check container ownership is unconfirmed.")
    return ids


def _run_reader(docker, arguments, environment):
    """Admit the Reader record; retain unknown Docker stderr privately for recovery."""
    try:
        result = subprocess.run(
            docker.docker_command_prefix + arguments,
            cwd=named.ROOT, env=environment, timeout=90,
            check=False, capture_output=True,
        )
    except subprocess.TimeoutExpired as error:
        raise OperationError("Reader check timed out; container retirement must be confirmed.") from error
    except OSError as error:
        raise OperationError(
            f"Reader check could not start ({type(error).__name__}, errno={error.errno})."
        ) from error
    if result.returncode:
        # Compose may surround the producer's record with lifecycle progress.
        # Admit exactly one bounded record, never the surrounding prose.
        records = [line for line in result.stderr.splitlines() if line.startswith(PREFIX)]
        reason = None
        if len(result.stderr) <= 65536 and len(records) == 1:
            reason = explanation(records[0], result.returncode, phases={"reader_check"})
        if reason is None:
            # Docker may fail before Python starts. Keep that producer's prose
            # private; ordinary Reader failures use the bounded record above.
            try:
                descriptor, receipt = tempfile.mkstemp(prefix="conix-reader-check-", suffix=".stderr")
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(result.stderr)
                reason = f"exit={result.returncode}; no admitted diagnostic. Private receipt: {receipt}"
            except OSError:
                reason = f"exit={result.returncode}; no admitted diagnostic; private receipt unavailable"
        raise OperationError("Reader check failed: " + reason, exit_code=result.returncode)
    if len(result.stdout) > 65536:
        raise OperationError("Reader check returned an oversized response.")
    return result.stdout


def check(docker, *, name, image, volume, database, rpc):
    """Check the actual Reader role/source, then retire only owned check work."""
    if IMAGE_ID.fullmatch(image or "") is None or VOLUME_NAME.fullmatch(volume or "") is None:
        raise OperationError("Select an exact prepared Reader image and private volume.")
    docker.require_rootless_engine()
    docker.require_role(image, "indexer")
    _admit_volume(docker, volume)
    for selected in (database, rpc):
        _admit_network(docker, selected)
    if database == rpc:
        raise OperationError("Reader needs distinct selected database and RPC bridges.")
    docker.verify_inputs(image, volume)

    token = secrets.token_hex(12)
    project = "conix-reader-" + name + "-" + token[:12]
    compose = [
        "compose", "--env-file", "/dev/null", "--project-directory", str(named.ROOT),
        "-p", project, "-f", str(named.ROOT / "compose" / "reader.yml"),
    ]
    environment = docker.docker_environment.copy()
    environment.update(
        CONIX_READER_IMAGE=image,
        CONIX_READER_VOLUME=volume,
        CONIX_DATABASE_NETWORK=database,
        CONIX_RPC_NETWORK=rpc,
        CONIX_READER_TOKEN=token,
    )
    try:
        configuration = json.loads(docker.run(
            *compose, "config", "--format", "json",
            capture=True, timeout=20, env=environment,
        ))
    except ValueError as error:
        raise OperationError("Docker Compose returned invalid Reader configuration.") from error
    admit_graph(
        configuration, image=image, volume=volume,
        database=database, rpc=rpc, token=token,
    )

    if _check_candidates(docker, project=project, image=image, token=token):
        raise OperationError("Reader check's unique project already contains a container.")

    primary = None
    try:
        output = _run_reader(
            docker, compose + ["run", "--rm", "--no-deps", "--pull", "never",
                              "-T", "--name", project + "-check", "reader"],
            environment,
        )
        try:
            record = json.loads(output)
            if (
                type(record) is not dict
                or set(record) != {"outcome", "source_id", "checkpoint", "generation"}
                or record["outcome"] != "reader_ready"
                or type(record["source_id"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", record["source_id"]) is None
                or (record["checkpoint"] is not None
                    and (type(record["checkpoint"]) is not int or record["checkpoint"] < 0))
                or (record["generation"] is not None and type(record["generation"]) is not str)
            ):
                raise ValueError()
        except (ValueError, TypeError) as error:
            raise OperationError("Reader check did not return an admitted source/checkpoint record.") from error
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            candidates = _check_candidates(docker, project=project, image=image, token=token)
            for identity in candidates:
                docker.run("container", "rm", "--force", identity, timeout=30)
            if _check_candidates(docker, project=project, image=image, token=token):
                raise OperationError("Reader check container retirement is unconfirmed.")
        except BaseException as error:
            if primary is None:
                raise OperationError("Reader check cleanup is unconfirmed.") from error
            print(
                f"Reader check cleanup is unconfirmed for owned project {project}.",
                file=sys.stderr,
            )

    # Report successful source/role admission only after owned check work is retired.
    print(json.dumps(record, sort_keys=True))
    return record
