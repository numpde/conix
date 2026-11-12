"""Retained Tier-0 service activation on one explicitly selected rootless Engine.

Compose declares the expected graph. Activation creates it STOPPED, admits
the actual container and borrowed resources, then starts only the admitted
container. A partial/uncertain creation remains inspectable for operator-
authorized retirement; never guess an ownership ID or delete a foreign item.
"""

import json
import re

from tools.containers.client import OperationError
from tools.containers.history import (
    ENGINE_ID, _admit_network, _admit_volume, _record, _selected_networks,
)


COMMAND = ["-I", "-m", "conix", "run"]


def _candidate_ids(docker, project):
    text = docker.run(
        "ps", "--all", "--no-trunc",
        "--filter", "label=com.docker.compose.project=" + project,
        "--filter", "label=com.docker.compose.service=indexer",
        "--format", "{{.ID}}", capture=True, timeout=20,
    )
    identities = text.splitlines()
    if any(ENGINE_ID.fullmatch(item) is None for item in identities):
        raise OperationError("Docker returned an incomplete retained-container identity.")
    return identities


def _owned(record, identity, project, image):
    try:
        labels = record["Config"]["Labels"]
        return (
            record["Id"] == identity and record["Image"] == image
            and labels["com.docker.compose.project"] == project
            and labels["com.docker.compose.service"] == "indexer"
        )
    except (KeyError, TypeError):
        return False


def _admit_created(record, *, identity, project, image, volume, networks):
    """Check actual grants, not just an anticipated Compose configuration."""
    try:
        config = record["Config"]
        host = record["HostConfig"]
        (mount,) = record["Mounts"]
        attached = record["NetworkSettings"]["Networks"]
        logging = host["LogConfig"]
        tmpfs = host["Tmpfs"]["/tmp"]
        allowed = (
            _owned(record, identity, project, image)
            and record["State"]["Status"] == "created"
            and config["User"] == "65532:65532"
            and config["Entrypoint"] == ["python"]
            and config["Cmd"] == COMMAND
            and config["Image"] == image
            and host["Init"] is True
            and host["ReadonlyRootfs"] is True
            and host["Privileged"] is False
            and set(host["CapDrop"]) == {"ALL"} and not host.get("CapAdd")
            and host["SecurityOpt"] == ["no-new-privileges:true"]
            and host["Memory"] == host["MemorySwap"] == 512 * 1024 * 1024
            and host["PidsLimit"] == 128 and host["NanoCpus"] == 10**9
            and host["RestartPolicy"]["Name"] == "no"
            and host["LogConfig"]["Type"] == "local"
            and logging["Config"].get("max-size") == "10m"
            and str(logging["Config"].get("max-file")) == "3"
            and "size=16m" in tmpfs.split(",")
            and "uid=65532" in tmpfs.split(",")
            and "gid=65532" in tmpfs.split(",")
            and not any(host.get(key) for key in (
                "Binds", "PortBindings", "Devices", "DeviceRequests", "PidMode",
            ))
            and host["PublishAllPorts"] is False
            and mount["Type"] == "volume" and mount["Name"] == volume
            and mount["Destination"] == "/run/conix" and mount["RW"] is False
            and _selected_networks(attached, networks)
        )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise OperationError("Cannot inspect the retained service's actual confinement.") from error
    if not allowed:
        raise OperationError("Created retained service violates its selected confinement.")


def activate(docker, *, project, compose_argv, environment, image, volume, database, rpc):
    """Admit image/inputs/networks, create stopped, verify, then start exactly it."""
    docker.require_rootless_engine()
    docker.require_role(image, "indexer")
    _admit_volume(docker, volume)
    database_id = _admit_network(docker, database)
    rpc_id = _admit_network(docker, rpc)
    if database_id == rpc_id:
        raise OperationError("Retained service requires distinct database and RPC networks.")
    docker.verify_inputs(image, volume)

    if _candidate_ids(docker, project):
        raise OperationError("Selected deployment already has an indexer container; inspect or remove it.")

    try:
        docker.run(*compose_argv, "create", "--no-build", "--no-recreate", "indexer",
                   env=environment, timeout=90)
    except OperationError as error:
        raise OperationError(
            "Retained service creation was not confirmed; inspect deployment/status "
            "before attempting another activation."
        ) from error

    candidates = _candidate_ids(docker, project)
    if len(candidates) != 1:
        raise OperationError("Cannot identify exactly one created retained indexer; do not start it.")

    (identity,) = candidates
    record = _record(docker, "container", identity)
    _admit_created(
        record, identity=identity, project=project, image=image,
        volume=volume, networks=((database, database_id), (rpc, rpc_id)),
    )
    # Docker can replace or detach selected resources after create; re-admit
    # borrowed identities immediately before the irreversible start boundary.
    _admit_volume(docker, volume)
    if _admit_network(docker, database) != database_id or _admit_network(docker, rpc) != rpc_id:
        raise OperationError("Selected service network identity changed before start.")
    docker.run("container", "start", identity, timeout=45)
    print(f"Started retained Conix service {project} ({identity}).")


def show_logs(docker, project):
    """Read a bounded tail only from a currently ownership-checked container."""
    identities = _candidate_ids(docker, project)
    if len(identities) != 1:
        raise OperationError("Select a deployment with exactly one retained indexer.")
    (identity,) = identities
    record = _record(docker, "container", identity)
    labels = record.get("Config", {}).get("Labels", {})
    if record.get("Id") != identity or type(labels) is not dict or (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "indexer"
    ):
        raise OperationError("Cannot establish retained log ownership.")
    docker.run("container", "logs", "--tail", "100", identity, timeout=20)
