"""Name stress runs for readers and publish explicit, reviewable summaries.

Private receipts and WARC remain authoritative and byte-preserved. Reports
contain selected facts and hashes, never recursive copies of producer output.
Docker ownership nonces have no role in the directory naming convention.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib

from tools.containers.client import OperationError
from tools.containers.diagnostics import admitted
from tools.fixtures.stress_protocol import BUNDLE_INPUTS

PRIVATE_RUNS = Path("out/indexer/stress/runs")
PUBLIC_RUNS = Path("qualification/indexer/stress/runs")
PHASES = {"capture": "manifest.json", "evidence-completion": "admission.json"}
SUBJECT = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
STAMP = re.compile(r"[0-9]{8}-[0-9]{6}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def _subject(value):
    if type(value) is not str or len(value) > 64 or SUBJECT.fullmatch(value) is None:
        raise OperationError("Stress subject must be one lowercase hyphen-separated name.")
    return value


def source_subject(source, reviewed_file):
    """Name public source facts; private input paths never supply report labels.

    Known fixture identities use their reviewed name. Other sources retain
    their declared numeric chain identity without guessing a network name.
    This affects presentation only; source admission still checks every hash.
    """
    chain_id = _count(source["chain_id"])
    _hash_file(reviewed_file, 65536)
    reviewed = json.loads(reviewed_file.read_bytes())
    if all(source[key] == reviewed[key] for key in ("chain_id", "genesis_hash", "address")):
        return _subject(reviewed_file.stem.replace("_", "-"))
    return "chain-" + str(chain_id)


def _private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o077:
        raise OperationError("Stress run directories must be owned private directories.")


def allocate_run(repository, subject, phase, *, when=None):
    """Allocate exclusively before effects; a timestamp collision never reuses data."""
    subject = _subject(subject)
    if phase not in PHASES:
        raise OperationError("Unknown stress artifact phase.")
    when = datetime.now(timezone.utc) if when is None else when
    if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
        raise OperationError("Stress artifact timestamps must use UTC.")
    relative = Path(when.strftime("%Y%m%d-%H%M%S")) / subject / phase
    published = repository / PUBLIC_RUNS / relative
    if published.exists() or published.is_symlink():
        raise OperationError("This dated stress run already has review evidence; select a new run time.")
    parent = repository / PRIVATE_RUNS
    for component in relative.parts[:-1]:
        parent /= component
        _private_directory(parent)
    folder = parent / phase
    folder.mkdir(mode=0o700)
    return folder


def _hash_file(path, maximum, *, allow_empty=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        minimum = 0 if allow_empty else 1
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or not minimum <= details.st_size <= maximum:
            raise OperationError("Stress evidence must be an owned bounded regular file.")
        return hashlib.file_digest(stream, "sha256").hexdigest(), details.st_size


def _count(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise OperationError("Stress report count is not a bounded nonnegative integer.")
    return value


def _digest(value):
    if type(value) is not str or HASH.fullmatch(value) is None:
        raise OperationError("Stress report lacks a SHA-256 identity.")
    return value


def report_facts(repository, folder, records, *, original_directory=None):
    """Project only known facts; raw stderr, URLs, samples and argv stay private."""
    relative = folder.relative_to(repository / PRIVATE_RUNS)
    if len(relative.parts) != 3 or STAMP.fullmatch(relative.parts[0]) is None or relative.parts[2] not in PHASES:
        raise OperationError("Stress evidence does not have a dated subject/phase layout.")
    (stamp, subject, phase) = relative.parts
    _subject(subject)
    status = records["capture"] if phase == "capture" else records["status"]
    if status not in {"complete", "partial", "unconfirmed", "unavailable"}:
        raise OperationError("Stress report has an unknown outcome.")
    if records["cleanup"]:
        status = "unavailable"
    receipt = PHASES[phase]
    (receipt_hash, _) = _hash_file(folder / receipt, 2 * 1048576)
    result = {
        "run": stamp, "subject": subject, "phase": phase, "status": status,
        "private_directory": folder.relative_to(repository).as_posix(),
        "receipt": {"file": receipt, "sha256": receipt_hash},
        "cleanup_failure_count": len(records["cleanup"]),
    }
    if original_directory is not None:
        original = Path(original_directory)
        if (len(original.parts) != 3 or original.parts[:2] not in (("out", "rpc-recordings"), ("out", "rpc-evidence"))
                or re.fullmatch(r"[0-9a-f]{24}", original.name) is None or original.name != records.get("owner")):
            raise OperationError("Historical stress location must identify its original owned capture or completion.")
        result["original_private_directory"] = original.as_posix()
    for key in ("source_sha256", "abi_sha256", "deployment_sha256", "source_id"):
        if key in records:
            result[key] = _digest(records[key])
    for key in ("revision", "capture_revision"):
        if key in records:
            value = records[key]
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
                raise OperationError("Stress report revision is malformed.")
            result[key] = value
    for key in ("image", "test_image", "indexer_image"):
        if key in records:
            value = records[key]
            if type(value) is not str or not value.startswith("sha256:"):
                raise OperationError("Stress report image must be immutable.")
            _digest(value[7:])
            result[key] = value
    if "source_dirty" in records:
        if type(records["source_dirty"]) is not bool:
            raise OperationError("Stress source status is malformed.")
        result["source_dirty"] = records["source_dirty"]
    if "input_sha256" in records:
        result["input_sha256"] = {name: _digest(records["input_sha256"][name])
                                  for name in BUNDLE_INPUTS if name in records["input_sha256"]}
    config = folder / ("deployment.toml" if phase == "capture" else "catalog.toml")
    if config.exists():
        _hash_file(config, 65536)
        selected = tomllib.loads(config.read_text())
        chain_id = _count(selected["chain"]["chain_id"])
        addresses = [contract["address"] for contract in selected["contracts"].values()]
        if any(type(address) is not str or re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None for address in addresses):
            raise OperationError("Stress report contract address is malformed.")
        result["source"] = {"chain_id": chain_id, "contracts": addresses}
    archives = ("application", "observer") if phase == "capture" else ("completion",)
    result["archives"] = {}
    for name in archives:
        archive = folder / (name + ".warc.gz")
        if not archive.exists() and not archive.is_symlink():
            if status == "complete":
                raise OperationError("Completed stress run lacks its retained archive.")
            continue
        (digest, size) = _hash_file(archive, 64 * 1048576, allow_empty=status != "complete")
        expected = records["export"].get(name, {}).get("sha256") if phase == "capture" else records.get("completion_sha256")
        if expected is not None and digest != _digest(expected):
            raise OperationError("Stress archive changed after its recorded verification.")
        if expected is None and status == "complete":
            raise OperationError("Completed stress run lacks its archive verification identity.")
        result["archives"][archive.name] = {"sha256": digest, "bytes": size,
                                           "verification": "verified" if expected is not None else "unconfirmed"}
    if "coverage" in records:
        result["coverage"] = {key: _count(records["coverage"][key]) for key in (
            "start_block", "final_block", "headers", "selectors", "log_cells", "reused", "acquired", "rechecks", "completion_exchanges",
        )}
    if "observer" in records:
        observation = records["observer"]
        result["observation"] = {"duration_ns": _count(observation["actual_span_ns"]),
                                 "samples": len(observation["samples"]),
                                 "seed_height": _count(observation["seed"]["header"]["height"]),
                                 "initial_height": _count(observation["samples"][0]["header"]["height"]),
                                 "final_height": _count(observation["samples"][-1]["header"]["height"])}
    if "processes" in records:
        outcomes = {}
        for operation in ("admit", "complete", "verify"):
            if operation not in records["processes"]:
                continue
            facts = records["processes"][operation]
            if (type(facts["exit_code"]) is not int or not 0 <= facts["exit_code"] <= 255
                    or type(facts["oom_killed"]) is not bool or type(facts["attachment_complete"]) is not bool):
                raise OperationError("Stress report process outcome is malformed.")
            outcomes[operation] = {key: facts[key] for key in ("exit_code", "oom_killed", "attachment_complete")}
        result["worker_outcomes"] = outcomes
    if "worker_exits" in records:
        result["worker_exits"] = {worker: _count(records["worker_exits"][worker]) for worker in ("gateway", "observer")
                                  if worker in records["worker_exits"]}
    if "process_continuity" in records:
        if records["process_continuity"] not in ("confirmed", "unconfirmed"):
            raise OperationError("Stress report process continuity is unknown.")
        result["process_continuity"] = records["process_continuity"]
    diagnostic = records.get("diagnostic")
    if type(diagnostic) is str:
        fields = admitted(diagnostic.encode("ascii"))
        if fields is not None:
            result["failure"] = fields
    return result


def publish_report(repository, folder, records, *, original_directory=None):
    """Publish an exclusive summary; never replace an earlier run's review evidence."""
    facts = report_facts(repository, folder, records, original_directory=original_directory)
    target = repository / PUBLIC_RUNS / folder.relative_to(repository / PRIVATE_RUNS) / "report.json"
    parent = repository
    for component in target.parent.relative_to(repository).parts:
        parent /= component
        parent.mkdir(mode=0o755, exist_ok=True)
        details = parent.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o022:
            raise OperationError("Stress review directories must be owned and protected from replacement.")
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".report-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write((json.dumps(facts, sort_keys=True, indent=2) + "\n").encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target
