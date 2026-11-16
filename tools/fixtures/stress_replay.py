"""Own Stage C: two offline measurements of the unchanged installed Conix CLI.

The selected Engine owns every disposable container and volume. Recorded WARC
evidence is immutable and no worker has public-network access. PostgreSQL
publisher and the original Reader remain the sole application authorities.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import tomllib

from tools.containers import named
from tools.containers.client import IMAGE_ID, OperationError, select_endpoint
from tools.containers.deployment import _candidate_ids
from tools.containers.process_evidence import require_running
from tools.containers.diagnostics import PHASES, error_record, explanation
from tools.fixtures.deployment import campaign_lock
from tools.fixtures.process import cancellation, clean_environment, owned_command, progress, retire_deployment
from tools.fixtures.stress_artifacts import PRIVATE_RUNS, allocate_run, publish_report, source_subject
from tools.fixtures.stress_capture import SOURCE_FILE, check_file, json_record, put_private, require
from tools.fixtures.stress_evidence import bundle_inputs
from tools.fixtures.stress_protocol import BUNDLE_INPUTS
from tools.wheelhouse.operations import SharedWheelhouse

ROOT = named.ROOT
OWNER_LABEL = "io.conix.stress-replay-owner"
ENGINE_ID = re.compile(r"[0-9a-f]{64}\Z")


def supplied(options):
    """Mandatory, literal, bounded measurement controls."""
    limits = {
        "stress_replay_delay_ms": (1, 5000),
        "stress_checkpoint_cadence_ms": (100, 60000),
        "stress_catchup_timeout_seconds": (1, 3600),
    }
    result = {}
    for key, (minimum, maximum) in limits.items():
        value = getattr(options, key, None)
        require(type(value) is str and value.isdecimal() and minimum <= int(value) <= maximum,
                f"Explicit {key.upper()} must be in {minimum}..{maximum}.")
        result[key] = int(value)
    require(options.capture_dir and options.evidence_dir,
            "CAPTURE_DIR and EVIDENCE_DIR must select retained A/B evidence.")
    require(options.context, "DOCKER_CONTEXT must select the rootless local Engine.")
    require(type(options.runner_image) is str and IMAGE_ID.fullmatch(options.runner_image),
            "RUNNER_IMAGE must name one immutable installed test-runner.")
    return result


def private_folder(input_value):
    """Reject symlink aliases, borrowed folders and group-writable receipts."""
    folder = Path(input_value).expanduser()
    if not folder.is_absolute():
        folder = ROOT / folder
    details = folder.lstat()
    require(stat.S_ISDIR(details.st_mode) and details.st_uid == os.geteuid()
            and not details.st_mode & 0o077,
            "Replay input must be a private owned directory.")
    return folder


def admitted_bundle(capture_dir, evidence_dir):
    """Bind Stage B's receipt and completion archive to exact retained Stage A bytes."""
    capture = private_folder(capture_dir)
    completion = private_folder(evidence_dir)
    payload, hashes = bundle_inputs(capture)
    receipt = completion / "admission.json"
    completed = completion / "completion.warc.gz"
    digest = check_file(receipt, 2 * 1048576)
    archived = check_file(completed, 64 * 1048576)
    record = json.loads(receipt.read_bytes())
    require(type(record) is dict and record.get("status") == "complete"
            and record.get("phase") == "completed" and not record.get("cleanup")
            and record.get("input_sha256") == hashes
            and record.get("completion_sha256") == archived
            and record.get("inspection", {}).get("status") == "verified"
            and record["inspection"]["completion_sha256"] == archived,
            "Replay requires one complete, fully verified Stage B admission.")
    source = json.loads((capture / "source.json").read_bytes())
    original = tomllib.loads((capture / "deployment.toml").read_text())
    require(record["coverage"]["start_block"] == original["chain"]["start_block"]
            and record["coverage"]["log_cells"] == record["coverage"]["selectors"] *
                (record["coverage"]["final_block"] - record["coverage"]["start_block"] + 1),
            "Stage B admitted coverage is incomplete.")
    archive = io.BytesIO(payload)
    with tarfile.open(fileobj=archive, mode="a") as members:
        for (name, path, size) in (
            ("completion.warc.gz", completed, 64 * 1048576),
            ("admission.json", receipt, 2 * 1048576),
        ):
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and not info.st_mode & 0o077 and 0 < info.st_size <= size,
                    "Retained replay attachment changed before transport.")
            header = tarfile.TarInfo(name)
            header.size, header.mode = info.st_size, 0o600
            with path.open("rb") as stream:
                members.addfile(header, stream)
    return archive.getvalue(), hashes, record, source, original


class ReplayAttempt:
    """Own one independent schema, named CLI, Reader and offline RPC schedule."""

    def __init__(self, docker, images, runner, folder, source, declaration, payload,
                 admission, controls, label, delay):
        self.docker, self.images, self.runner = docker, images, runner
        self.folder, self.source, self.declaration = folder, source, declaration
        self.payload, self.admission, self.controls = payload, admission, controls
        self.label, self.delay = label, delay
        self.owner = secrets.token_hex(12)
        self.project = "conix-stress-replay-" + self.owner
        self.name = "stress-replay-" + self.owner
        self.profile = ROOT / "config/deployments" / self.name
        self.prefix = ["compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
                       "-p", self.project, "-f", str(ROOT / "compose/stress-replay.yml")]
        self.environment = docker.docker_environment.copy()
        self.environment.update(
            CONIX_REPLAY_OWNER=self.owner,
            CONIX_REPLAY_DELAY_MS=str(delay),
            CONIX_TEST_RUNNER_IMAGE=runner,
            CONIX_POSTGRES_IMAGE=images["postgres"],
            CONIX_FOUNDRY_IMAGE=images["foundry"],
        )
        self.phase = "preflight"
        self.sequence = 0
        self.touched = False
        self.profile_created = False
        self.consumer_started = False
        self.consumer_retired = False
        self.sampler_id = None
        self.sampler_retired = False
        self.result_exported = False
        self.replay_id = None
        self.records = {
            "label": label, "delay_ms": delay, "owner": self.owner,
            "engine": docker.endpoint, "project": self.project, "deployment": self.name,
            "status": "unavailable", "cleanup": [], "phases": [],
        }
        self.folder.mkdir(mode=0o700)
        json_record(self.folder / "recovery.json", {
            "project": self.project, "deployment": self.name, "owner": self.owner,
            "endpoint": docker.endpoint, "volumes": [self.project + "_" + name
                for name in ("pgdata", "evidence", "results")],
            "status": "resources require exact owner verification before retirement",
        })

    def child(self, argv, label, *, payload=None, timeout=90, environment=None):
        self.phase = label
        self.sequence += 1
        destination = self.folder / f"{self.sequence:03d}"
        return owned_command(
            argv, output=destination.with_suffix(".out"),
            error_file=destination.with_suffix(".err"),
            env=self.environment if environment is None else environment,
            timeout=timeout, label=label, payload=payload,
        )

    def make(self, action):
        require(select_endpoint(self.declaration["docker_context"], "command line",
                                working_dir=ROOT) == self.docker.endpoint,
                "Saved replay Engine differs from the selected rootless Engine.")
        return self.child(["make", "--no-print-directory", action, "DEPLOYMENT=" + self.name],
                          "Replay " + action, environment=clean_environment(), timeout=660)

    def compose(self, *args, capture=False, timeout=90):
        if capture:
            return self.docker.run(*self.prefix, *args, env=self.environment,
                                   capture=True, timeout=timeout)
        return self.child(self.docker.docker_command_prefix + self.prefix + list(args),
                          "Replay fixture " + args[0], timeout=timeout)

    def admit_graph(self, graph):
        """Reject every extra bridge, service, mount, capability or image role."""
        require(set(graph["services"]) == {"postgres", "bootstrap", "volume-init", "replay"}
                and set(graph["networks"]) == {"tests", "rpc"}
                and set(graph["volumes"]) == {"pgdata", "evidence", "results"},
                "Replay Compose graph differs from its exact offline authority.")
        grants = {
            "postgres": (self.images["postgres"], {"tests"}, {("pgdata", "/var/lib/postgresql/data")}),
            "bootstrap": (self.images["postgres"], {"tests"}, set()),
            "volume-init": (self.runner, set(), {("evidence", "/init/evidence"), ("results", "/init/results")}),
            "replay": (self.runner, {"rpc"}, {("evidence", "/evidence")}),
        }
        for name, service in graph["services"].items():
            image, networks, mounts = grants[name]
            require(service.get("image") == image and service.get("pull_policy") == "never"
                    and service.get("read_only") is True
                    and set(service.get("networks", {})) == networks
                    and service.get("labels", {}).get(OWNER_LABEL) == self.owner
                    and set(service.get("cap_drop", [])) == {"ALL"}
                    and service.get("security_opt") == ["no-new-privileges:true"]
                    and not any(service.get(key) for key in
                                ("privileged", "ports", "build", "devices", "secrets", "configs", "pid")),
                    "Replay service exceeds its selected confinement.")
            selected = service.get("volumes", [])
            require({(v.get("source"), v.get("target")) for v in selected} == mounts
                    and all(v.get("type") == "volume" for v in selected),
                    "Replay service has unselected filesystem authority.")
            if name != "volume-init":
                require(not service.get("cap_add") and not service.get("network_mode"),
                        "Networked replay service has unselected privilege.")
            else:
                require(service.get("network_mode") == "none"
                        and service.get("user") == "0:0"
                        and set(service.get("cap_add", [])) == {"CHOWN"},
                        "Replay volume initializer exceeded its narrow grant.")
        for name in ("tests", "rpc"):
            network = graph["networks"][name]
            require(network.get("name") == self.project + "_" + name
                    and network.get("internal") is True
                    and network.get("labels", {}).get(OWNER_LABEL) == self.owner
                    and not network.get("external") and not network.get("driver_opts"),
                    "Replay bridge grants unintended external connectivity.")
        for name in ("pgdata", "evidence", "results"):
            volume = graph["volumes"][name]
            require(volume.get("name") == self.project + "_" + name
                    and volume.get("labels", {}).get(OWNER_LABEL) == self.owner
                    and not volume.get("external") and not volume.get("driver_opts"),
                    "Replay volume has unexpected ownership.")
        replay = graph["services"]["replay"]
        require(replay.get("user") == "65532:65532"
                and replay.get("entrypoint") == ["python", "-E", "-s", "/tests/stress_replay.py", "serve"]
                and replay.get("command") == ["--delay-ms", str(self.delay)]
                and str(replay.get("mem_limit")) in ("1g", "1073741824")
                and str(replay.get("memswap_limit")) in ("1g", "1073741824")
                and replay.get("restart") == "no",
                "Replay worker image, latency policy or resource limits changed.")

    def preflight(self):
        require(not self.profile.exists() and not self.profile.is_symlink(),
                "Replay refuses an existing generated named deployment.")
        require(not _candidate_ids(self.docker, "conix-" + self.name),
                "Replay refuses an existing original Conix process.")
        for kind, names in (("network", ("tests", "rpc")),
                            ("volume", ("pgdata", "evidence", "results"))):
            for suffix in names:
                name = self.project + "_" + suffix
                found = self.docker.run(kind, "ls", "--filter", "name=" + name,
                                        "--format", "{{.Name}}", capture=True).splitlines()
                require(name not in found, "Replay fixture resource name is already owned.")
        graph = json.loads(self.compose("config", "--format", "json", capture=True))
        self.admit_graph(graph)

    def owned(self, name):
        """Inspect one immutable service ID; never infer ownership from its name."""
        identities = self.docker.run(
            "ps", "--all", "--no-trunc", "--filter",
            "label=com.docker.compose.project=" + self.project,
            "--filter", "label=com.docker.compose.service=" + name,
            "--format", "{{.ID}}", capture=True,
        ).splitlines()
        require(len(identities) == 1 and ENGINE_ID.fullmatch(identities[0]),
                "Replay service identity is missing or ambiguous.")
        record = json.loads(self.docker.run(
            "container", "inspect", "--format", "{{json .}}", identities[0], capture=True))
        expected = self.runner if name == "replay" else self.images["postgres"]
        require(record["Id"] == identities[0] and record["Image"] == expected
                and record["Config"]["Labels"].get(OWNER_LABEL) == self.owner,
                "Replay service image or immutable owner changed.")
        return identities[0], record

    def volume(self, name):
        identity = self.project + "_" + name
        record = json.loads(self.docker.run(
            "volume", "inspect", "--format", "{{json .}}", identity, capture=True))
        require(record["Name"] == identity and record["Driver"] == "local"
                and record["Labels"].get(OWNER_LABEL) == self.owner,
                "Replay volume ownership is unavailable.")
        return identity

    def create_profile(self):
        self.phase = "private named replay deployment"
        self.profile.mkdir(mode=0o700)
        self.profile_created = True
        for part in ("abi", "secrets"):
            (self.profile / part).mkdir(mode=0o700)
        source = deepcopy(self.declaration)
        source.update(database_network=self.project + "_tests",
                      rpc_network=self.project + "_rpc")
        source["chain"] = dict(source["chain"], rpc_url="http://replay:8080")
        put_private(self.profile / "deployment.toml", named.render_deployment(source).encode())
        put_private(self.profile / "abi/pay_to_play.json", self.source["abi"])
        for role in ("owner", "reader", "runtime"):
            dsn = f"postgresql://conix_{role}:conix-disposable-{role}@postgres:5432/conix_rpc_test\n"
            put_private(self.profile / "secrets" / (role + ".dsn"), dsn.encode())
        self.records["profile_sha256"] = check_file(self.profile / "deployment.toml", 65536)

    def stage(self):
        self.phase = "networkless evidence admission"
        self.touched = True
        self.compose("run", "--rm", "--no-deps", "--pull", "never",
                     "--interactive=false", "-T", "volume-init", timeout=75)
        arguments = self.docker.docker_command_prefix + [
            "run", "--rm", "--interactive", "--pull", "never", "--network", "none",
            "--log-driver", "none", "--read-only", "--user", "65532:65532",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "1g", "--memory-swap", "1g",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,uid=65532,gid=65532,mode=0700,size=256m",
            "--mount", "type=volume,source=" + self.volume("evidence") + ",target=/evidence,volume-nocopy",
            "--entrypoint", "python", self.runner, "-E", "-s", "/tests/stress_replay.py", "stage",
        ]
        raw = self.child(arguments, "Replay offline immutable admission", payload=self.payload,
                         timeout=180)
        selected = json.loads(raw)
        require(selected["status"] == "admitted" and selected["source_id"] == self.admission["source_id"]
                and selected["start"] == self.admission["coverage"]["start_block"]
                and selected["final"] == self.admission["coverage"]["final_block"],
                "Contained replay worker admitted another coverage interval.")
        self.records["admission"] = selected

    def boot(self):
        self.phase = "offline replay and database startup"
        self.compose("up", "--detach", "--no-build", "--pull", "never",
                     "--wait", "--wait-timeout", "120", "postgres", "replay", timeout=160)
        self.compose("run", "--rm", "--no-deps", "--pull", "never",
                     "--interactive=false", "-T", "bootstrap", timeout=90)
        self.replay_id, replay = self.owned("replay")
        require_running(self.docker, replay, "Offline replay service")
        self.create_profile()
        for action in ("deployment/prepare", "deployment/database/schema/init",
                       "deployment/config/check", "deployment/reader/prepare"):
            self.make(action)
        self.records["indexer_image"] = named.inspect_prepared(self.name).image
        self.records["reader_volume"] = named.inspect_reader_prepared(self.name).volume

    def sample_arguments(self):
        reader = named.inspect_reader_prepared(self.name)
        require(reader is not None and reader.image == self.records["indexer_image"],
                "Original Reader selection changed before sampling.")
        return self.docker.docker_command_prefix + [
            "run", "--detach", "--pull", "never", "--name", "conix-replay-sampler-" + self.owner,
            "--label", OWNER_LABEL + "=" + self.owner, "--network", self.project + "_rpc",
            "--network", self.project + "_tests", "--log-driver", "none",
            "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "64",
            "--memory", "1g", "--memory-swap", "1g", "--cpus", "1",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,uid=65532,gid=65532,mode=0700,size=16m",
            "--mount", "type=volume,source=" + reader.volume +
                        ",target=/run/conix,readonly,volume-nocopy",
            "--mount", "type=volume,source=" + self.volume("evidence") +
                        ",target=/evidence,readonly,volume-nocopy",
            "--mount", "type=volume,source=" + self.volume("results") +
                        ",target=/results,volume-nocopy",
            "--entrypoint", "python", self.runner, "-E", "-s",
            "/tests/stress_replay_probe.py",
            "--sample-ms", str(self.controls["stress_checkpoint_cadence_ms"]),
            "--catchup-seconds", str(self.controls["stress_catchup_timeout_seconds"]),
            "--start-wait-seconds", "180",
        ]

    def sampler(self):
        self.phase = "Reader sampler startup"
        self.sampler_id = self.docker.run(*self.sample_arguments()[len(self.docker.docker_command_prefix):],
                                          capture=True, timeout=30).strip()
        require(ENGINE_ID.fullmatch(self.sampler_id), "Owned sampler identity was not returned.")
        deadline = time.monotonic() + 120
        while True:
            self.sampler_state(running=True)
            try:
                raw = self.docker.run(
                    "exec", "--user", "65532:65532", self.sampler_id,
                    "python", "-E", "-s", "/tests/stress_replay_probe.py", "ready",
                    capture=True, timeout=15,
                )
            except OperationError:
                require(time.monotonic() < deadline,
                        "Original Reader sampler did not establish pre-release readiness.")
                time.sleep(0.2)
                continue
            ready = json.loads(raw)
            require(ready.get("status") == "ready"
                    and ready.get("source_id") == self.admission["source_id"]
                    and ready.get("initial_head") == self.source["initial_head"]
                    and ready.get("cadence_ms") == self.controls["stress_checkpoint_cadence_ms"],
                    "Original Reader sampler readiness selects different replay evidence.")
            self.records["sampler_ready"] = ready
            break

    def sampler_state(self, *, running=False):
        require(self.sampler_id is not None, "Owned sampler was not created.")
        record = json.loads(self.docker.run("container", "inspect", "--format",
                                           "{{json .}}", self.sampler_id, capture=True))
        require(record["Id"] == self.sampler_id and record["Image"] == self.runner
                and record["Config"]["Labels"].get(OWNER_LABEL) == self.owner
                and record["HostConfig"]["LogConfig"]["Type"] == "none",
                "Reader sampler identity or confinement changed.")
        if running and not record["State"]["Running"]:
            # Logging is deliberately disabled. Retain daemon exit/OOM facts
            # and the exact owned identity rather than a generic liveness loss.
            self.records["sampler_terminal"] = {
                "id": record["Id"], "exit": record["State"]["ExitCode"],
                "oom": record["State"]["OOMKilled"],
            }
            raise OperationError("Reader sampler stopped before release; "
                                 "daemon exit=" + str(record["State"]["ExitCode"]) +
                                 "; OOM=" + str(record["State"]["OOMKilled"]),
                                 exit_code=record["State"]["ExitCode"])
        return record

    def status(self):
        raw = self.docker.run(
            "exec", "--user", "65532:65532", self.replay_id, "python", "-E", "-s",
            "/tests/stress_replay.py", "status", capture=True, timeout=15)
        record = json.loads(raw)
        require(type(record["head"]) is int and type(record["released"]) is bool
                and type(record["elapsed_ns"]) is int and type(record["requests"]) is int,
                "Replay returned an invalid bounded status.")
        return record

    def original(self):
        ids = _candidate_ids(self.docker, "conix-" + self.name)
        require(len(ids) == 1, "Original retained Conix process identity is missing.")
        record = json.loads(self.docker.run(
            "container", "inspect", "--format", "{{json .}}", ids[0], capture=True))
        prepared = named.inspect_prepared(self.name)
        require(prepared is not None and record["Id"] == ids[0]
                and record["Image"] == prepared.image
                and record["Config"]["User"] == "65532:65532"
                and record["Config"]["Cmd"] == ["-I", "-m", "conix", "run"]
                and set(record["NetworkSettings"]["Networks"]) ==
                    {self.project + "_tests", self.project + "_rpc"},
                "Original installed Conix execution or grants changed.")
        require_running(self.docker, record, "Original installed Conix")
        return ids[0]

    @contextmanager
    def lifecycle(self, identity, since):
        """Subscribe to Engine events; retrospective rings alone are insufficient."""
        self.phase = "original process continuity"
        path = self.folder / "lifecycle.jsonl"
        errors = self.folder / "lifecycle.err"
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=self.admission["coverage"]["final_block"] -
                    self.admission["coverage"]["start_block"] + 3600,
        )
        argv = self.docker.docker_command_prefix + [
            "events", "--since", since.isoformat(), "--until", deadline.isoformat(),
            "--filter", "container=" + identity, "--filter", "event=start",
            "--filter", "event=die", "--filter", "event=restart",
            "--format", "{{json .}}",
        ]
        with path.open("xb") as output, errors.open("xb") as error_file:
            path.chmod(0o600)
            errors.chmod(0o600)
            process = subprocess.Popen(
                argv, cwd=ROOT, env=self.docker.docker_environment,
                stdin=subprocess.DEVNULL, stdout=output, stderr=error_file,
            )
        primary = None
        try:
            started = time.monotonic()
            while True:
                require(process.poll() is None, "Engine lifecycle stream ended before observation.")
                raw = path.read_text()
                if raw.endswith("\n"):
                    self.lifecycle_evidence(identity, raw)
                    break
                require(time.monotonic() - started < 10,
                        "Engine failed to confirm original process start.")
                time.sleep(0.05)
            yield process
            require(process.poll() is None, "Engine lifecycle stream ended during replay.")
            self.records["lifecycle"] = self.lifecycle_evidence(identity, path.read_text())
        except BaseException as error:
            primary = error
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                if primary is None:
                    raise OperationError("Engine event subscriber required forced retirement.")
                primary.add_note("Engine event subscriber retirement is unconfirmed.")

    def lifecycle_evidence(self, identity, raw):
        require(len(raw) <= 65536, "Engine lifecycle record exceeded its private bound.")
        events = [json.loads(line) for line in raw.splitlines()]
        selected = [event for event in events if event.get("Type") == "container"
                    and event.get("Actor", {}).get("ID") == identity
                    and event.get("Action") in ("start", "die", "restart")]
        actions = [entry["Action"] for entry in selected]
        require(actions == ["start"] and all(type(event.get("timeNano")) is int
                                           for event in selected),
                "Original indexer did not have one continuous Engine process.")
        return {"events": [{"action": entry["Action"], "time_ns": entry["timeNano"]}
                           for entry in selected]}

    def measured(self):
        self.phase = "bounded baseline or delayed measurement"
        self.sampler()
        first = self.status()
        require(first["head"] == self.source["initial_head"],
                "Replay head differs from the admitted first timed observation.")
        require(first["released"] is False and first["elapsed_ns"] == 0,
                "Replay pacing started during fixture preparation.")
        time.sleep(1)
        second = self.status()
        require(second["head"] == first["head"] and second["released"] is False
                and second["elapsed_ns"] == 0,
                "Delayed setup advanced the recorded chain clock.")
        release_utc = datetime.now(timezone.utc) - timedelta(seconds=2)
        gate = threading.Event()

        def launch_original():
            gate.wait()
            self.records["launch_request_host_ns"] = time.monotonic_ns()
            try:
                self.make("deployment/up")
            finally:
                self.records["launch_completion_host_ns"] = time.monotonic_ns()

        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(launch_original)
            try:
                self.phase = "common replay release"
                sent = time.monotonic_ns()
                release = json.loads(self.docker.run(
                    "exec", "--user", "65532:65532", self.replay_id,
                    "python", "-E", "-s", "/tests/stress_replay.py", "release",
                    capture=True, timeout=15,
                ))
                self.records["release_ns"] = release["released_ns"]
                self.records["launch_trigger_host_ns"] = sent
            finally:
                gate.set()
            task.result(timeout=690)
        self.consumer_started = True
        identity = self.original()
        self.records["indexer_id"] = identity
        self.records["launch_request_offset_host_ns"] = (
            self.records["launch_request_host_ns"] - self.records["launch_trigger_host_ns"])
        record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}",
                                            identity, capture=True))
        self.records["indexer_started_utc"] = record["State"]["StartedAt"]
        with self.lifecycle(identity, release_utc):
            # The wall budget follows the *recorded observation interval*
            # plus the explicit catch-up grant, not an assumed block rate.
            budget = (self.records["admission"]["window_ns"] // 1000000000 +
                      self.controls["stress_catchup_timeout_seconds"] + 180)
            self.phase = "original Reader and checkpoint measurement"
            raw = self.docker.run("container", "wait", self.sampler_id,
                                  timeout=budget, capture=True).strip()
            require(raw.isdecimal() and 0 <= int(raw) <= 255,
                    "Reader sampler exit status is unavailable.")
            state = self.sampler_state()
            require(not state["State"]["Running"] and state["State"]["ExitCode"] == int(raw)
                    and not state["State"]["OOMKilled"], "Reader sampler exit or OOM is unconfirmed.")
            self.records["sampler_exit"] = int(raw)
            self.records["service"] = self.status()
            require(self.records["service"]["errors"] == 0
                    and self.records["service"]["first_rpc_elapsed_ns"] is not None,
                    "Original process did not produce valid replay RPC requests.")
            self.original()
        self.docker.run("cp", self.sampler_id + ":/results/result.json",
                        str(self.folder / "result.json"), timeout=30)
        self.records["result_sha256"] = check_file(
            self.folder / "result.json", 2 * 1048576, imported=True)
        self.result_exported = True
        result = json.loads((self.folder / "result.json").read_bytes())
        require(result["source_id"] == self.admission["source_id"]
                and result["start"] == self.admission["coverage"]["start_block"]
                and result["final"] == self.admission["coverage"]["final_block"]
                and result["status"] in ("complete", "incomplete_catchup")
                and result["window"] and result["first_checkpoint_ns"] is not None,
                "Measured original Reader report is missing or unadmitted.")
        require(int(raw) == 0, "Original Reader verification worker exited unsuccessfully.")
        result["service"] = self.records["service"]
        result["original_process"] = self.records["lifecycle"]
        result["indexer_image"] = self.records["indexer_image"]
        result["process_launch"] = {
            "requested_host_monotonic_ns": self.records["launch_request_host_ns"],
            "completed_host_monotonic_ns": self.records["launch_completion_host_ns"],
            "trigger_offset_host_ns": self.records["launch_request_offset_host_ns"],
            "started_utc": self.records["indexer_started_utc"],
            "release_server_monotonic_ns": self.records["release_ns"],
        }
        json_record(self.folder / "measurement.json", result)
        self.records["status"] = result["status"]
        self.records["measurement_sha256"] = check_file(self.folder / "measurement.json", 2 * 1048576)
        return result

    def verify_owned_graph(self):
        identities = self.docker.run(
            "ps", "--all", "--no-trunc", "--filter",
            "label=com.docker.compose.project=" + self.project,
            "--format", "{{.ID}}", capture=True,
        ).splitlines()
        for identity in identities:
            require(ENGINE_ID.fullmatch(identity), "Fixture container identity is truncated.")
            record = json.loads(self.docker.run(
                "container", "inspect", "--format", "{{json .}}", identity, capture=True))
            require(record["Id"] == identity and record["Config"]["Labels"].get(OWNER_LABEL) == self.owner
                    and record["Config"]["Labels"].get("com.docker.compose.service") in
                        ("postgres", "bootstrap", "replay", "volume-init"),
                    "Replay fixture resource ownership changed before cleanup.")
        for kind, names in (("network", ("tests", "rpc")),
                            ("volume", ("pgdata", "evidence", "results"))):
            for suffix in names:
                name = self.project + "_" + suffix
                found = self.docker.run(kind, "ls", "--filter", "name=" + name,
                                        "--format", "{{.Name}}", capture=True).splitlines()
                if name in found:
                    record = json.loads(self.docker.run(kind, "inspect", name, capture=True))[0]
                    require(record["Name"] == name and record["Labels"].get(OWNER_LABEL) == self.owner,
                            "Replay owned bridge/volume changed during cleanup.")

    def cleanup(self):
        """Preserve stage and result volumes if their exact publication is uncertain."""
        failures = []
        try:
            if self.profile_created:
                retire_deployment(self.make, self.docker, self.name, self.records)
                self.consumer_retired = True
            if self.sampler_id is not None:
                state = self.sampler_state()
                if state["State"]["Running"]:
                    self.docker.run("container", "stop", "--time", "20",
                                    self.sampler_id, timeout=35)
                self.sampler_state()
                self.docker.run("container", "rm", self.sampler_id, timeout=30)
                self.sampler_retired = True
            if self.touched:
                self.verify_owned_graph()
                self.compose("down", "--remove-orphans", "--timeout", "30", timeout=90)
                for name in ("pgdata", "evidence", "results"):
                    if name == "results" and not self.result_exported:
                        continue
                    self.docker.run("volume", "rm", self.volume(name), timeout=30)
            if self.profile_created:
                if named.inspect_reader_prepared(self.name) is not None:
                    self.make("deployment/reader/remove")
                prepared = named.inspect_prepared(self.name)
                if prepared is not None:
                    self.docker.remove_inputs(prepared.volume)
                    named.clear_prepared(self.name, expected_volume=prepared.volume)
                shutil.rmtree(self.profile)
        except BaseException as error:
            failures.append({"phase": self.phase, "type": type(error).__name__})
        self.records["cleanup"] = failures
        return failures


def comparison(observations):
    """Report trajectories and start/catch-up timing; no binary pace criterion."""
    result = {}
    for key in ("baseline", "slowdown"):
        measured = observations[key]
        window = measured["window"]
        result[key] = {
            "window_samples": len(window), "window_ns": measured["window_ns"],
            "first_checkpoint_ns": measured["first_checkpoint_ns"],
            "first_rpc_ns": measured["service"]["first_rpc_elapsed_ns"],
            "launch_request_offset_host_ns": measured.get("process_launch", {}).get(
                "trigger_offset_host_ns"),
            "initial_lag": window[0]["checkpoint_lag"],
            "final_sampled_lag": window[-1]["checkpoint_lag"],
            "max_sampled_lag": max((r["checkpoint_lag"] for r in window
                                    if r["checkpoint_lag"] is not None), default=None),
            "sampled_backlog_change": (
                None if window[0]["checkpoint_lag"] is None or window[-1]["checkpoint_lag"] is None
                else window[-1]["checkpoint_lag"] - window[0]["checkpoint_lag"]),
            "catchup_status": measured["status"],
            "reader_verified": measured["reader"] is not None,
            "request_count": measured["service"]["requests"],
            "actual_wait_ns": measured["service"]["actual_wait_ns"],
            "added_latency_ms": measured["service"]["delay_ms"],
        }
    return result


def run_replay(options, *, client_cls, pinned_images):
    """One maintained operation creates two separately owned real-effect runs."""
    require(os.geteuid() != 0, "Recorded stress replay requires the ordinary host operator.")
    controls = supplied(options)
    payload, input_hashes, admission, source, declaration = admitted_bundle(
        options.capture_dir, options.evidence_dir)
    require(options.context == declaration["docker_context"],
            "Replay Engine must match the retained deployment's selected Engine.")
    subject = source_subject(source, SOURCE_FILE)
    root = ROOT / PRIVATE_RUNS
    with campaign_lock(root), cancellation(), tempfile.TemporaryDirectory(prefix="conix-replay-client-") as temporary:
        config = Path(temporary)
        put_private(config / "config.json", b'{"auths":{},"proxies":{}}\n')
        docker = client_cls(select_endpoint(options.context, "command line", working_dir=ROOT), config)
        docker.require_rootless_engine()
        docker.require_role(options.runner_image, "test-runner")
        images = pinned_images()
        docker.require_image(images["postgres"])
        parent = allocate_run(ROOT, subject, "replay")
        put_private(parent / "catalog.toml",
                    (private_folder(options.evidence_dir) / "catalog.toml").read_bytes())
        result = {"status": "unavailable", "source_id": admission["source_id"],
                  "source_sha256": input_hashes["source.json"],
                  "abi_sha256": input_hashes["abi.json"],
                  "input_sha256": input_hashes, "completion_sha256": admission["completion_sha256"],
                  "capture_revision": admission["capture_revision"],
                  "runner_image": options.runner_image, "controls": controls,
                  "runs": {}, "cleanup": [], "comparison": None}
        json_record(parent / "operation.json", result)
        measurements = {}
        primary = None
        for label, delay in (("baseline", 0), ("slowdown", controls["stress_replay_delay_ms"])):
            source_inputs = private_folder(options.capture_dir)
            capture = json.loads((source_inputs / "manifest.json").read_bytes())
            run = ReplayAttempt(docker, images, options.runner_image, parent / label,
                                dict(source, abi=(source_inputs / "abi.json").read_bytes(),
                                     initial_head=capture["observer"]["samples"][0]["header"]["height"]),
                                declaration, payload, admission, controls, label, delay)
            try:
                run.preflight()
                run.stage()
                run.boot()
                measured = run.measured()
                measurements[label] = measured
            except BaseException as error:
                primary = primary or error
                run.records["failure"] = error_record(error).decode("ascii")
            finally:
                run.records["cleanup"] = run.cleanup()
                result["runs"][label] = run.records
                if run.records["cleanup"]:
                    result["cleanup"].extend(run.records["cleanup"])
                    if primary is None:
                        primary = OperationError("Replay owned resource cleanup is unconfirmed.")
            if primary is not None:
                break
        if set(measurements) == {"baseline", "slowdown"}:
            result["comparison"] = comparison(measurements)
            result["status"] = ("complete" if all(m["status"] == "complete" for m in measurements.values())
                                else "partial")
        if primary is not None:
            result["diagnostic"] = error_record(primary).decode("ascii")
            result["status"] = "unavailable"
        json_record(parent / "result.json", result)
        try:
            report = publish_report(ROOT, parent, result)
            progress("Stress replay review report: " + str(report.relative_to(ROOT)))
        except BaseException as error:
            if primary is None:
                primary = error
        for failed in result["cleanup"]:
            progress("Replay cleanup unconfirmed: " + str(failed.get("phase", "unknown")) +
                     " (" + str(failed.get("type", "unavailable")) + ")")
        progress("Stress replay " + result["status"] + "; private reports: " + str(parent))
        if primary is not None:
            diagnostic = error_record(primary)
            detail = explanation(diagnostic, getattr(primary, "exit_code", None), phases=PHASES)
            raise OperationError("Stress replay failed: " + detail,
                                 diagnostic=diagnostic,
                                 exit_code=getattr(primary, "exit_code", None)) from primary
