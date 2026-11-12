"""Shared admission of exact Docker input volumes and internal bridges.

The scanner's one-shot history worker, the retained deployment and the
named initializer all use the same resource facts. Each lifecycle still owns
its own container activation, error handling, and resource retirement.
"""

import json
import re

from tools.containers.client import CONTEXT_NAME, OperationError


ENGINE_ID = re.compile(r"[a-f0-9]{64}\Z")
VOLUME_NAME = re.compile(r"conix-inputs-([a-f0-9]{24})\Z")


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


