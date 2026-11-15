"""Own explicit public-network measurement; ordinary CI remains offline.

The selected immutable test image reads a reviewed public source snapshot.
It receives egress on one owned bridge, no credentials or host mounts, and
can write only private captured output. Collection never edits test fixtures.
"""

from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from tools.containers.client import OperationError
from tools.containers.diagnostics import PREFIX, PHASES, error_record, explanation
from tools.fixtures.deployment import campaign_lock
from tools.fixtures.process import ROOT, CampaignError, cancellation, owned_command, progress

SOURCES = ROOT / "tests/fixtures/evm/network_rate_sources.json"
OWNER = "io.conix.network-rate-measurement"
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")

SOURCE_CHECK = """import hashlib
from pathlib import Path
from conix.process_diagnostic import emit_unexpected
try:
    print(hashlib.sha256(Path('/tests/fixtures/evm/network_rate_sources.json').read_bytes()).hexdigest())
except Exception as error:
    emit_unexpected(phase='network_measure', error=error, effect='not_started')
    raise SystemExit('Selected test image cannot supply the reviewed network source snapshot.') from None
"""


def require(condition, message):
    if not condition:
        raise OperationError(message)


def admit_observations(raw, source_bytes):
    """Admit bounded source-bound facts before they can become offline fixtures."""
    try:
        require(len(raw) <= 1048576, "Network-rate receipt exceeds the admitted output budget.")
        result = json.loads(raw)
        sources = json.loads(source_bytes)
        require(result["kind"] == "observed_network_rates" and result["source_sha256"] == hashlib.sha256(source_bytes).hexdigest(),
                "Network-rate worker did not measure the selected source snapshot.")
        started = datetime.fromisoformat(result["started_at_utc"])
        completed = datetime.fromisoformat(result["completed_at_utc"])
        require(started.utcoffset() == timedelta(0) and completed.utcoffset() == timedelta(0) and completed >= started,
                "Network-rate receipt has no ordered UTC observation interval.")
        expected = {network["id"]: network for network in sources["networks"]}
        rows = result["networks"]
        require(type(rows) is list and len(rows) == len(expected) and {row["id"] for row in rows} == set(expected),
                "Network-rate receipt omitted, duplicated or added a source.")
        labels = {"head" if value == 100 else f"height_{value}_percent" for value in sources["sampling"]["height_positions_percent"]}

        def failure(record):
            require(type(record) is dict and set(record) <= {"cause", "code", "location"}
                    and type(record.get("cause")) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", record["cause"]),
                    "Network-rate failure has no bounded cause.")
            if "code" in record:
                require(type(record["code"]) is int and abs(record["code"]) < 2**31, "Network-rate failure code is malformed.")
            if "location" in record:
                require(type(record["location"]) is str and re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]{0,63}\.py:[1-9][0-9]{0,5}|unavailable)", record["location"]),
                        "Network-rate failure source metadata is not admitted.")

        def header(record):
            require(type(record) is dict and all(type(record[key]) is int and record[key] >= 0 for key in ("height", "timestamp", "transactions", "response_bytes"))
                    and all(type(record[key]) is str and HASH.fullmatch(record[key]) for key in ("hash", "parent_hash"))
                    and type(record["elapsed_ms"]) in (int, float) and math.isfinite(record["elapsed_ms"]) and record["elapsed_ms"] >= 0,
                    "Network-rate header evidence is malformed.")
            require(0 < record["response_bytes"] <= sources["sampling"]["response_limit_bytes"],
                    "Network-rate sampled header exceeds the measurement byte budget.")
            require(("timestamp_ms" in record) == ("timestamp_field" in record), "Network-rate timestamp metadata is incomplete.")
            if "timestamp_ms" in record:
                require(type(record["timestamp_ms"]) is int and record["timestamp_ms"] // 1000 == record["timestamp"]
                        and record.get("timestamp_field") in {"timestampMs", "timestampMilliseconds"},
                        "Network-rate millisecond timestamp is inconsistent.")

        for row in rows:
            selected = expected[row["id"]]
            require(type(row["chain_id"]) is int and row["chain_id"] == selected["chain_id"] and row["endpoint"] == selected["rpc_url"]
                    and row["status"] in {"observed", "partial", "unavailable", "retired", "no_public_endpoint"}
                    and row["phase"] in {"selection", "identity", "genesis", "head", "windows", "completed"}
                    and row["identity_assurance"] in {"not_checked", "chain_id_checked", "chain_id_checked_genesis_observed"},
                    "Network-rate receipt changed a selected source or its status.")
            require((row["status"] == "retired") == selected["retired"], "Network-rate receipt changed retired-network coverage.")
            require((row["status"] == "no_public_endpoint") == (selected["rpc_url"] is None and not selected["retired"]),
                    "Network-rate endpoint availability changed without an observation.")
            if selected["rpc_url"] is None and not selected["retired"]:
                require(row["status"] == "no_public_endpoint", "Unavailable endpoint was presented as a measurement.")
            if selected["retired"]:
                require(row["status"] == "retired" and not row["windows"], "Retired network was presented as a current measurement.")
            if row["status"] in ("observed", "partial"):
                genesis = row["genesis"]
                if genesis["status"] == "observed":
                    header(genesis["header"])
                    require(genesis["header"]["height"] == 0 and row["identity_assurance"] == "chain_id_checked_genesis_observed",
                            "Network-rate genesis observation does not match its assurance.")
                else:
                    require(genesis["status"] == "unavailable" and row["identity_assurance"] == "chain_id_checked",
                            "Network-rate genesis gap has an unknown identity outcome.")
                    failure(genesis["failure"])
                header(row["head"])
                require(row["phase"] == "completed", "Completed network observations have an inconsistent phase.")
                require(len(row["windows"]) == len(labels) and {window["id"] for window in row["windows"]} == labels,
                        "Network-rate receipt omitted a historical window.")
            if row["status"] == "unavailable":
                failure(row["failure"])
                require(not any(window["status"] == "observed" for window in row["windows"]),
                        "Unavailable network has contradictory completed measurements.")
            if row["status"] == "partial":
                require(row["genesis"]["status"] == "unavailable" or any(window["status"] == "unavailable" for window in row["windows"]), "Partial network lacks an unavailable observation.")
            if row["status"] == "observed":
                require(row["genesis"]["status"] == "observed" and all(window["status"] == "observed" for window in row["windows"]), "Incomplete observations were presented as a complete network measurement.")
            for window in row["windows"]:
                require(window["id"] in labels and window["status"] in ("observed", "unavailable"), "Network-rate window has an unknown outcome.")
                if window["status"] == "unavailable":
                    require(set(window) == {"id", "status", "failure"}, "Unavailable window carries contradictory rate evidence.")
                    failure(window["failure"])
                    continue
                first, last = window["first"], window["last"]
                header(first)
                header(last)
                position = 100 if window["id"] == "head" else int(window["id"].split("_")[1])
                end = row["head"]["height"] * position // 100
                require(last["height"] == end and first["height"] == max(0, end - sources["sampling"]["window_span_blocks"]),
                        "Network-rate window does not occupy the selected historical position.")
                span = last["height"] - first["height"]
                require(type(window["height_span"]) is int and window["height_span"] == span and span > 0
                        and type(window["duration_ms"]) is int and window["duration_ms"] > 0
                        and type(window["mean_numbered_block_interval_ms"]) in (int, float)
                        and math.isfinite(window["mean_numbered_block_interval_ms"])
                        and math.isclose(window["mean_numbered_block_interval_ms"], window["duration_ms"] / span),
                        "Network-rate cadence is not supported by its observation span.")
                if window["timestamp_resolution_ms"] == 1:
                    require(first.get("timestamp_field") == last.get("timestamp_field") and first.get("timestamp_field") is not None,
                            "Network-rate endpoints use incompatible millisecond timestamp fields.")
                field = "timestamp_ms" if window["timestamp_resolution_ms"] == 1 else "timestamp"
                factor = 1 if field == "timestamp_ms" else 1000
                require(window["timestamp_resolution_ms"] in (1, 1000) and window["duration_ms"] == (last[field] - first[field]) * factor,
                        "Network-rate interval differs from its timestamp evidence.")
                require(0 < len(window["tail"]) <= sources["sampling"]["contiguous_tail_blocks"], "Network-rate contiguous sample exceeds its reviewed budget.")
                tail = window["tail"]
                expected_heights = list(range(max(first["height"], end - sources["sampling"]["contiguous_tail_blocks"] + 1), end + 1))
                require([record["height"] for record in tail] == expected_heights, "Network-rate tail is not the selected contiguous sample.")
                for index, record in enumerate(tail):
                    header(record)
                    if index:
                        require(record["parent_hash"] == tail[index-1]["hash"] and record["timestamp"] >= tail[index-1]["timestamp"],
                                "Network-rate tail ancestry is inconsistent.")
                        previous = tail[index-1]
                        if "timestamp_ms" in previous and "timestamp_ms" in record and previous["timestamp_field"] == record["timestamp_field"]:
                            require(record["timestamp_ms"] >= previous["timestamp_ms"], "Network-rate subsecond tail time moved backward.")
                anchor_fields = ("height", "hash", "parent_hash", "timestamp", "transactions", "timestamp_ms", "timestamp_field")
                require(tuple(tail[-1].get(key) for key in anchor_fields) == tuple(last.get(key) for key in anchor_fields),
                        "Network-rate tail does not end at its window anchor.")
                selector = window["selector_probe"]
                require(selector["status"] in ("observed", "unavailable") and selector["filter"] == {
                    "blockHash": last["hash"], "address": sources["selector_probe"]["address"], "topics": [sources["selector_probe"]["topic0"]],
                }, "Network-rate selector cost belongs to another query.")
                if selector["status"] == "unavailable":
                    require(set(selector) == {"status", "filter", "failure"}, "Unavailable selector carries a completed query cost.")
                    failure(selector["failure"])
                if selector["status"] == "observed":
                    require(type(selector["log_count"]) is int and selector["log_count"] >= 0
                            and type(selector["elapsed_ms"]) in (int, float) and math.isfinite(selector["elapsed_ms"]) and selector["elapsed_ms"] >= 0,
                            "Network-rate selected-log cost is unavailable.")
        if "collection_artifact" in result:
            artifact = result["collection_artifact"]
            require(artifact["source_sha256"] == result["source_sha256"] and type(artifact["runner_image"]) is str
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["runner_image"]),
                    "Network-rate provenance does not identify its immutable measured artifact.")
        return result
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise OperationError("Network-rate receipt has malformed or missing evidence fields.") from error


def worker_arguments(docker, image, owner, name, network):
    """One immutable read-only test worker; caller explicitly selects its network."""
    return docker.docker_command_prefix + ["run", "--rm", "--pull", "never", "--name", name,
        "--label", OWNER + "=" + owner, "--network", network, "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--pids-limit", "32", "--memory", "512m", "--memory-swap", "512m",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,uid=65532,gid=65532,mode=0700,size=8m", "--entrypoint", "python", image]


def collect(docker, image):
    """Collect under exact ownership and publish only after confirmed retirement."""
    require(os.geteuid() != 0, "Network-rate measurement requires the ordinary host operator.")
    docker.require_rootless_engine()
    docker.require_role(image, "test-runner")
    source_bytes = SOURCES.read_bytes()
    sources = json.loads(source_bytes)
    for network in sources["networks"]:
        if network["rpc_url"] is not None:
            endpoint = urlsplit(network["rpc_url"])
            require(endpoint.scheme == "https" and endpoint.hostname and not endpoint.username and not endpoint.password
                    and not endpoint.query and not endpoint.fragment, "Network-rate sources must be reviewed keyless HTTPS endpoints.")
    root = ROOT / "out/network-rate-measurements"
    with campaign_lock(root), cancellation():
        owner = secrets.token_hex(12)
        folder = root / owner
        folder.mkdir(mode=0o700)
        network = "conix-rate-measurement-" + owner
        container = "conix-rate-probe-" + owner
        label = OWNER + "=" + owner
        primary = None
        cleanup_errors = []
        attempted = False
        result = None
        output = folder / "probe.out"
        try:
            provenance = {"image": image, "endpoint": docker.endpoint, "source_sha256": hashlib.sha256(source_bytes).hexdigest(), "owner": owner}
            descriptor = os.open(folder / "operator.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(provenance, stream)
            attempted = True
            error_file = folder / "source-check.err"
            check = worker_arguments(docker, image, owner, container, "none") + ["-I", "-c", SOURCE_CHECK]
            selected_hash = owned_command(check, output=folder / "source-check.out", error_file=error_file,
                                          env=docker.docker_environment, timeout=30, label="Network-source grant verification")
            require(selected_hash.strip() == hashlib.sha256(source_bytes).hexdigest().encode("ascii"),
                    "Selected test image carries another network-source snapshot; no egress was granted.")
            docker.run("network", "create", "--driver", "bridge", "--label", label, network, capture=True)
            error_file = folder / "probe.err"
            argv = worker_arguments(docker, image, owner, container, network) + ["-E", "-s", "/tests/network_rate_probe.py"]
            raw = owned_command(argv, output=output, error_file=error_file, env=docker.docker_environment,
                                timeout=1500, label="Public network-rate measurement")
            result = admit_observations(raw, source_bytes)
        except BaseException as error:
            primary = error
            if isinstance(error, CampaignError):
                try:
                    with error_file.open("rb") as stream:
                        captured = stream.read(65537)
                    lines = captured.splitlines() if len(captured) <= 65536 else []
                    records = [line for line in lines if line.startswith(PREFIX)]
                    detail = explanation(records[0], error.exit_code, phases={"network_measure"}) if len(records) == 1 else None
                    if detail is not None:
                        stage = "Network-source grant verification" if error_file.name == "source-check.err" else "Network-rate collection"
                        primary = OperationError(stage + ": " + detail, diagnostic=records[0], exit_code=error.exit_code)
                        primary.__cause__ = error
                except OSError:
                    pass
        if attempted:
            cleanup_phase = "measurement container retirement"
            try:
                ids = docker.run("ps", "--all", "--no-trunc", "--filter", "name=^/" + container + "$", "--format", "{{.ID}}", capture=True).splitlines()
                for identity in ids:
                    record = json.loads(docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
                    require(record["Id"] == identity and record["Config"]["Labels"].get(OWNER) == owner and record["Config"]["Image"] == image,
                            "Network-rate container ownership changed; dependencies were retained.")
                    docker.run("container", "rm", "--force", identity, capture=True)
                cleanup_phase = "measurement bridge retirement"
                names = docker.run("network", "ls", "--filter", "name=" + network, "--format", "{{.Name}}", capture=True).splitlines()
                if network in names:
                    record = json.loads(docker.run("network", "inspect", network, capture=True))[0]
                    require(record["Name"] == network and record["Labels"].get(OWNER) == owner, "Network-rate bridge ownership changed; it was retained.")
                    docker.run("network", "rm", network, capture=True)
            except BaseException as error:
                detail = str(error) if type(error) is OperationError else explanation(error_record(error), getattr(error, "exit_code", None), phases=PHASES)
                cleanup_errors.append(cleanup_phase + ": " + detail)
        for detail in cleanup_errors:
            try:
                progress("Network-rate cleanup unconfirmed: " + detail)
            except OSError:
                pass
        if primary is not None:
            if type(primary) in (OperationError, CampaignError) or isinstance(primary, KeyboardInterrupt):
                raise primary
            record = error_record(primary)
            raise OperationError(explanation(record, None, phases=PHASES), diagnostic=record) from primary
        require(not cleanup_errors, "Network-rate evidence was collected but cleanup is unconfirmed; private output was retained.")
        output.replace(folder / "observations.json")
        counts = {status: sum(row["status"] == status for row in result["networks"]) for status in ("observed", "partial", "unavailable", "retired", "no_public_endpoint")}
        print("Network-rate population collection completed; individual gaps remain explicit: " + json.dumps(counts))
        print("Private observations: " + str(folder / "observations.json"))
