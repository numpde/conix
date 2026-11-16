"""Own bounded completion of a retained capture through one confined worker.

Exact files travel over private stdin; WARC travels over private stdout with
daemon logging disabled. No host mount, SQL service, volume or listener is
needed. The original capture is immutable and every attempt has its own ID.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tarfile
import tempfile
import tomllib

from tools.containers import named
from tools.containers.client import OperationError, select_endpoint
from tools.containers.diagnostics import PHASES, error_record, explanation
from tools.containers.process_evidence import failure, retain_attached_exit
from tools.fixtures.deployment import campaign_lock
from tools.fixtures.process import CampaignError, cancellation, owned_command, progress
from tools.fixtures.stress_capture import check_file, json_record, put_private, require
from tools.fixtures.stress_protocol import CAPTURE_INPUTS, RECEIPT

ROOT = named.ROOT
OWNER = "io.conix.stress-evidence-owner"


def bundle_inputs(path):
    """Project the retained named declaration without reading current deployment files."""
    folder = Path(path).expanduser()
    if not folder.is_absolute():
        folder = ROOT / folder
    details = folder.lstat()
    require(stat.S_ISDIR(details.st_mode) and details.st_uid == os.geteuid() and not details.st_mode & 0o077,
            "Select an owned private retained capture directory.")
    hashes = {name: check_file(folder / name, maximum) for (name, maximum) in CAPTURE_INPUTS.items()}
    manifest = json.loads((folder / "manifest.json").read_bytes())
    source = json.loads((folder / "source.json").read_bytes())
    original = tomllib.loads((folder / "deployment.toml").read_text())
    require(manifest["capture"] == "complete", "Partial captures cannot be completed for replay.")
    for (name, field) in (("source.json", "source_sha256"), ("abi.json", "abi_sha256"), ("deployment.toml", "deployment_sha256")):
        require(hashes[name] == manifest[field], "Capture source snapshot hash differs from its manifest.")
    for stream in ("application", "observer"):
        require(manifest["export"][stream]["sha256"] == hashes[stream + ".warc.gz"],
                "Capture archive differs from its qualified export.")
    selection = {"chain": dict(original["chain"], rpc_url=source["rpc_url"]),
                 "limits": original["limits"],
                 "contracts": {alias: dict(contract, abi_file="abi.json") for (alias, contract) in original["contracts"].items()}}
    projected = named._project_catalog(named.Declaration(name="stress-evidence", docker_context="unused", source=selection)).encode()
    hashes["catalog.toml"] = hashlib.sha256(projected).hexdigest()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as bundle:
        for name in (*CAPTURE_INPUTS, "catalog.toml"):
            data = projected if name == "catalog.toml" else (folder / name).read_bytes()
            require(hashlib.sha256(data).hexdigest() == hashes[name], "Retained input changed while preparing its transport.")
            member = tarfile.TarInfo(name)
            (member.size, member.mode) = len(data), 0o600
            bundle.addfile(member, io.BytesIO(data))
    return payload.getvalue(), hashes


def supplied(options):
    values = {}
    for (name, minimum, maximum) in (("stress_completion_seconds", 1, 1800),
                                   ("stress_exchanges", 1, 16384), ("stress_archive_bytes", 1048576, 64 * 1048576)):
        value = getattr(options, name, None)
        require(type(value) is str and value.isdecimal() and minimum <= int(value) <= maximum,
                f"Explicit {name.upper()} must be in {minimum}..{maximum}.")
        values[name] = int(value)
    require(options.capture_dir, "CAPTURE_DIR must select retained Stage A evidence.")
    require(options.context, "DOCKER_CONTEXT must select one named local Engine.")
    require(options.runner_image and re.fullmatch(r"sha256:[0-9a-f]{64}", options.runner_image),
            "RUNNER_IMAGE must select an immutable test-runner image.")
    return values


class Completion:
    """One attempt owns sequential workers and one temporary egress bridge."""

    def __init__(self, docker, image, folder, owner, selection):
        self.docker = docker
        self.image = image
        self.folder = folder
        self.owner = owner
        self.selection = selection
        self.network = "conix-evidence-" + owner
        self.network_attempted = False
        self.containers = {}
        self.records = {"status": "unavailable", "owner": owner, "image": image, "processes": {}, "cleanup": {}}

    def cleanup_failure(self, resource, error):
        self.records["cleanup"][resource] = {
            "diagnostic": error_record(error).decode("ascii"), "exit_code": getattr(error, "exit_code", None),
        }

    def arguments(self, operation):
        name = "conix-evidence-" + operation + "-" + self.owner
        network = self.network if operation == "complete" else "none"
        return self.docker.docker_command_prefix + [
            "run", "--interactive", "--pull", "never", "--name", name,
            "--label", OWNER + "=" + self.owner, "--network", network, "--log-driver", "none",
            "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "32", "--memory", "1g",
            "--memory-swap", "1g", "--cpus", "1",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,uid=65532,gid=65532,mode=0700,size=256m",
            "--entrypoint", "python", self.image, "-E", "-s", "/tests/stress_complete.py", operation,
            "--seconds", str(self.selection["stress_completion_seconds"]),
            "--exchanges", str(self.selection["stress_exchanges"]),
            "--archive-bytes", str(self.selection["stress_archive_bytes"]),
        ]

    def owned_container(self, name, network):
        ids = self.docker.run("ps", "--all", "--no-trunc", "--filter", "name=^/" + name + "$",
                              "--format", "{{.ID}}", capture=True).splitlines()
        require(len(ids) <= 1, "Completion container identity is ambiguous.")
        if not ids:
            return None
        identity = ids[0]
        require(re.fullmatch(r"[0-9a-f]{64}", identity), "Completion container identity is incomplete.")
        record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
        require(record["Id"] == identity and record["Name"] == "/" + name and record["Image"] == self.image
                and record["Config"]["Labels"].get(OWNER) == self.owner
                and record["HostConfig"]["NetworkMode"] == network
                and record["HostConfig"]["LogConfig"]["Type"] == "none",
                "Completion container ownership or attached-output grant changed.")
        return record

    def retire(self, operation):
        attachment = self.containers[operation]
        name = "conix-evidence-" + operation + "-" + self.owner
        network = self.network if operation == "complete" else "none"
        record = self.owned_container(name, network)
        if record is None:
            return
        if record["State"]["Running"]:
            self.docker.run("container", "stop", "--time", "20", record["Id"], timeout=30)
            record = self.owned_container(name, network)
            require(record is not None and not record["State"]["Running"], "Completion worker retirement is unconfirmed.")
        facts = retain_attached_exit(
            self.docker, record, output=attachment["output"], error_file=attachment["errors"],
            output_limit=attachment["limit"], attachment_complete=attachment["complete"],
        )
        self.records["processes"][operation] = facts
        # Retain the exact exit and attached files before removal. Admission
        # never treats confirmed removal as evidence of producer success.
        self.owned_container(name, network)
        self.docker.run("container", "rm", record["Id"], timeout=20)
        require(self.owned_container(name, network) is None, "Completion worker removal is unconfirmed.")
        return facts

    def worker(self, operation, payload):
        output = self.folder / ("completion.warc.gz" if operation == "complete" else operation + ".json")
        errors = self.folder / (operation + ".err")
        limit = self.selection["stress_archive_bytes"] if operation == "complete" else 1048576
        self.containers[operation] = {"output": output, "errors": errors, "limit": limit, "complete": False}
        primary = None
        try:
            owned_command(self.arguments(operation), output=output, error_file=errors,
                          env=self.docker.docker_environment, payload=payload,
                          timeout=self.selection["stress_completion_seconds"] + 90 if operation == "complete" else 90,
                          label="Stress evidence " + operation,
                          binary_output_limit=limit if operation == "complete" else None)
            self.containers[operation]["complete"] = True
        except BaseException as error:
            primary = error
            self.containers[operation]["complete"] = isinstance(error, CampaignError) and error.exit_code is not None
        try:
            facts = self.retire(operation)
        except BaseException as error:
            if primary is None:
                primary = error
            else:
                self.cleanup_failure(operation, error)
        else:
            if facts is not None and facts["exit_code"] != 0:
                if primary is None:
                    primary = failure(facts, "Evidence " + operation)
                try:
                    progress(f"Stress evidence {operation}: exit={facts['exit_code']}; OOM={facts['oom_killed']}; "
                             f"attachment_complete={facts['attachment_complete']}.")
                except OSError:
                    primary.add_note("Terminal outcome reporting failed; retained producer facts remain available.")
            if primary is None:
                require(facts is not None, "Completion producer disappeared before its exit evidence was retained.")
        if primary is not None:
            raise primary
        return output, errors

    def start_network(self):
        self.network_attempted = True
        self.docker.run("network", "create", "--driver", "bridge", "--label", OWNER + "=" + self.owner,
                        self.network, capture=True)

    def cleanup(self):
        for operation in self.containers:
            try:
                self.retire(operation)
            except BaseException as error:
                self.cleanup_failure(operation, error)
        if self.network_attempted:
            try:
                raw = self.docker.run("network", "ls", "--filter", "name=^" + self.network + "$",
                                      "--format", "{{.Name}}", capture=True).splitlines()
                require(raw in ([], [self.network]), "Completion bridge identity is ambiguous.")
                if raw:
                    record = json.loads(self.docker.run("network", "inspect", "--format", "{{json .}}", self.network, capture=True))
                    require(record["Name"] == self.network and record["Driver"] == "bridge"
                            and record["Labels"].get(OWNER) == self.owner and not record["Containers"],
                            "Completion bridge ownership or retirement is unconfirmed.")
                    self.docker.run("network", "rm", record["Id"], timeout=20)
            except BaseException as error:
                self.cleanup_failure("network", error)


def admit_result(folder, hashes, selection):
    errors = (folder / "complete.err").read_bytes()
    require(len(errors) <= 65536, "Completion control output exceeded its byte grant.")
    lines = [line[len(RECEIPT):] for line in errors.splitlines() if line.startswith(RECEIPT)]
    require(len(lines) == 1, "Completion worker did not supply exactly one admission receipt.")
    result = json.loads(lines[0])
    require(type(result) is dict and set(result) == {
        "status", "phase", "input_sha256", "source_id", "capture_revision", "coverage", "completion_sha256",
    }, "Completion receipt contains unsupported control fields.")
    require(type(result["source_id"]) is str and re.fullmatch(r"[0-9a-f]{64}", result["source_id"])
            and type(result["capture_revision"]) is str and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", result["capture_revision"])
            and type(result["coverage"]) is dict and set(result["coverage"]) == {
                "start_block", "final_block", "headers", "selectors", "log_cells", "reused", "acquired", "rechecks", "completion_exchanges",
            } and all(type(value) is int and 0 <= value < 2**63 for value in result["coverage"].values()),
            "Completion receipt lacks bounded source and coverage facts.")
    require(result["status"] == "complete" and result["phase"] == "completed"
            and result["input_sha256"] == hashes, "Completion receipt is incomplete or selects different retained inputs.")
    digest = check_file(folder / "completion.warc.gz", selection["stress_archive_bytes"])
    require(digest == result["completion_sha256"], "Completion stream differs from its producer receipt.")
    return result


def verification_bundle(payload, archive):
    bundle = io.BytesIO(payload)
    with tarfile.open(fileobj=bundle, mode="a") as stream:
        member = tarfile.TarInfo("completion.warc.gz")
        (member.size, member.mode) = archive.stat().st_size, 0o600
        with archive.open("rb") as source:
            stream.addfile(member, source)
    return bundle.getvalue()


def run_completion(options, *, client_cls):
    require(os.geteuid() != 0, "Stress evidence completion requires an ordinary non-root operator.")
    selection = supplied(options)
    try:
        (payload, hashes) = bundle_inputs(options.capture_dir)
    except (ValueError, KeyError, TypeError) as error:
        raise OperationError("Retained capture selection is malformed; no acquisition was started.", diagnostic=error_record(error)) from error
    root = ROOT / "out/rpc-evidence"
    with campaign_lock(root), cancellation(), tempfile.TemporaryDirectory(prefix="conix-evidence-client-") as temporary:
        config = Path(temporary)
        put_private(config / "config.json", b'{"auths":{},"proxies":{}}\n')
        docker = client_cls(select_endpoint(options.context, "command line", working_dir=ROOT), config)
        docker.require_rootless_engine()
        docker.require_role(options.runner_image, "test-runner")
        owner = secrets.token_hex(12)
        folder = root / owner
        folder.mkdir(mode=0o700)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as bundle:
            projected = bundle.extractfile("catalog.toml").read(65537)
        require(len(projected) <= 65536, "Projected catalog exceeds its transport byte grant.")
        put_private(folder / "catalog.toml", projected)
        completion = Completion(docker, options.runner_image, folder, owner, selection)
        completion.records.update({"input_sha256": hashes, "selection": selection})
        json_record(folder / "operation.json", completion.records)
        primary = None
        try:
            completion.worker("admit", payload)  # No egress exists before A eligibility passes.
            eligible = json.loads((folder / "admit.json").read_bytes())
            require(eligible["status"] == "eligible" and eligible["input_sha256"] == hashes,
                    "Offline admission selected different retained inputs.")
            completion.start_network()
            completion.worker("complete", payload)
            result = admit_result(folder, hashes, selection)
            completion.worker("verify", verification_bundle(payload, folder / "completion.warc.gz"))
            verified = json.loads((folder / "verify.json").read_bytes())
            require(verified["status"] == "verified" and verified["completion_sha256"] == result["completion_sha256"]
                    and verified["completion_exchanges"] == result["coverage"]["completion_exchanges"],
                    "Independent completion archive inspection is unavailable.")
            for field in ("start_block", "final_block", "headers", "selectors", "log_cells", "rechecks"):
                require(verified["coverage"][field] == result["coverage"][field],
                        "Rebuilt archive coverage differs from the completion receipt.")
            completion.records.update(result, inspection=verified)
        except BaseException as error:
            primary = error
        completion.cleanup()
        if completion.records["cleanup"]:
            completion.records["status"] = "unavailable"
            if primary is None:
                primary = OperationError("Completion resource retirement is unconfirmed; evidence retained.")
        if primary is not None:
            completion.records["status"] = "unavailable"
            completion.records["diagnostic"] = error_record(primary).decode("ascii")
        published = False
        publication_failure = None
        try:
            json_record(folder / "admission.json", completion.records)
            published = True
        except BaseException as error:
            publication_failure = error
            if primary is None:
                primary = error
        try:
            if publication_failure is not None:
                detail = explanation(error_record(publication_failure), None, phases=PHASES)
                progress("Evidence admission publication failed: " + detail)
            receipt = folder / ("admission.json" if published else "operation.json")
            status = completion.records["status"] if published else "unavailable"
            progress("Stress evidence " + status + "; private receipt: " + str(receipt))
            for (resource, outcome) in completion.records["cleanup"].items():
                detail = explanation(outcome["diagnostic"].encode("ascii"), outcome["exit_code"], phases=PHASES)
                progress("Completion " + resource + " cleanup is unconfirmed: " + detail)
        except OSError as error:
            if primary is None:
                primary = error
        if primary is not None:
            diagnostic = error_record(primary)
            detail = explanation(diagnostic, getattr(primary, "exit_code", None), phases=PHASES)
            raise OperationError("Stress evidence completion failed: " + detail,
                                 exit_code=getattr(primary, "exit_code", None), diagnostic=diagnostic) from primary
