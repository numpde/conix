"""Own one bounded Fuji stress capture without replay or evidence completion.

The original installed Conix process and ordinary named deployment operator
remain authoritative. This test owns only its disposable PostgreSQL fixture,
two recorded public-RPC streams, clock observations and retained artifacts.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time
import tomllib

from tools.containers import named
from tools.containers.client import OperationError, select_endpoint
from tools.containers.deployment import _candidate_ids
from tools.fixtures.deployment import campaign_lock
from tools.fixtures.process import cancellation, clean_environment, owned_command, progress
from tools.wheelhouse.operations import SharedWheelhouse

ROOT = named.ROOT
SOURCE_FILE = ROOT / "tests/fixtures/evm/fuji_pay_to_play.json"
PROFILE = ROOT / "config/deployments/fuji-acceptance"
OWNER_LABEL = "io.conix.stress-owner"
HASH = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise OperationError(message)


def supplied(options):
    """Explicit and finite capture parameters; no implicit unlimited grant."""
    specifications = {
        "stress_duration_seconds": (1, 600),
        "stress_poll_cadence_ms": (100, 60000),
        "stress_max_gap_ms": (100, 120000),
        "stress_clock_tolerance_ms": (1, 3000),
        "stress_archive_bytes": (1048576, 67108864),
        "stress_exchanges": (4, 4096),
        "stress_start_wait_seconds": (30, 900),
    }
    values = {}
    for field, (minimum, maximum) in specifications.items():
        raw = getattr(options, field, None)
        require(type(raw) is str and raw.isdecimal()
                and minimum <= int(raw) <= maximum,
                f"Explicit {field.upper()} must be in {minimum}..{maximum}.")
        values[field] = int(raw)
    count = (values["stress_duration_seconds"] * 1000
             + values["stress_poll_cadence_ms"] - 1) // values["stress_poll_cadence_ms"] + 2
    require(count <= values["stress_exchanges"], "Observer exchange budget cannot cover the observation window.")
    require(values["stress_max_gap_ms"] >= values["stress_poll_cadence_ms"],
            "Maximum successful-observation gap must allow at least one scheduled interval.")
    return values


def selected_source():
    """Bind one reviewed public source, ABI and existing 17-event catalog."""
    raw = SOURCE_FILE.read_bytes()
    source = json.loads(raw)
    profile = tomllib.loads((PROFILE / "deployment.toml").read_text())
    abi = (PROFILE / "abi/pay_to_play.json").read_bytes()
    signatures = {
        item["name"] + "(" + ",".join(field["type"] for field in item["inputs"]) + ")"
        for item in json.loads(abi) if item.get("type") == "event"
    }
    require(hashlib.sha256(abi).hexdigest() == source["abi_sha256"]
            and len(signatures) == 17
            and signatures == set(profile["contracts"]["payment"]["events"])
            and profile["contracts"]["payment"]["address"].lower() == source["address"]
            and profile["chain"]["chain_id"] == source["chain_id"]
            and profile["chain"]["genesis_hash"] == source["genesis_hash"],
            "Reviewed Fuji source, chain, contract or ABI selection changed.")
    return source, profile, raw, abi


def put_private(path, content):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def json_record(path, record):
    """Replace only our own private receipt after the final outcome is known."""
    raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    require(len(raw) <= 2097152, "Capture manifest exceeds the bounded control record.")
    temporary = path.with_suffix(".new")
    put_private(temporary, raw)
    os.replace(temporary, path)


def check_file(path, maximum):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and not info.st_mode & 0o077 and 0 < info.st_size <= maximum,
            "Exported capture artifact lacks bounded private file ownership.")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StressCapture:
    def __init__(self, docker, images, options, root, selection):
        self.docker = docker
        self.images = images
        self.options = options
        self.selection = selection
        self.owner = secrets.token_hex(12)
        self.name = "stress-" + self.owner
        self.project = "conix-stress-" + self.owner
        self.folder = root / self.owner
        self.folder.mkdir(mode=0o700)
        self.profile_dir = ROOT / "config/deployments" / self.name
        self.source, self.profile, self.source_raw, self.abi = selected_source()
        self.environment = docker.docker_environment.copy()
        self.environment.update({
            "CONIX_STRESS_OWNER": self.owner,
            "CONIX_POSTGRES_IMAGE": images["postgres"],
            "CONIX_STRESS_DURATION_SECONDS": str(selection["stress_duration_seconds"]),
            "CONIX_STRESS_CADENCE_MS": str(selection["stress_poll_cadence_ms"]),
            "CONIX_STRESS_MAX_GAP_MS": str(selection["stress_max_gap_ms"]),
            "CONIX_STRESS_START_WAIT_SECONDS": str(selection["stress_start_wait_seconds"]),
            "CONIX_STRESS_ARCHIVE_BYTES": str(selection["stress_archive_bytes"]),
            "CONIX_STRESS_EXCHANGES": str(selection["stress_exchanges"]),
        })
        self.compose = ["compose", "--env-file", "/dev/null",
                        "--project-directory", str(ROOT), "-p", self.project,
                        "-f", str(ROOT / "compose/stress-capture.yml")]
        self.runner = None
        self.fixture_attempted = False
        self.profile_created = False
        self.prepared = False
        self.launched = False
        self.archive_exported = set()
        self.retired = set()
        self.consumer_retired = False
        self.phase = "preflight"
        self.steps = 0
        self.records = {
            "version": 1, "owner": self.owner, "engine": docker.endpoint,
            "selected_parameters": selection, "source_sha256": hashlib.sha256(self.source_raw).hexdigest(),
            "abi_sha256": hashlib.sha256(self.abi).hexdigest(),
            "capture": "unconfirmed", "export": {}, "cleanup": [],
        }

    def child(self, argv, label, *, environment=None, timeout=90):
        self.steps += 1
        self.phase = label
        file = self.folder / f"{self.steps:03d}"
        return owned_command(argv, output=file.with_suffix(".out"),
                             error_file=file.with_suffix(".err"),
                             env=environment if environment is not None else self.environment,
                             timeout=timeout, label=label)

    def make(self, action):
        require(select_endpoint(self.profile["docker_context"], "command line", working_dir=ROOT)
                == self.docker.endpoint, "Selected rootless Docker Engine changed.")
        return self.child(
            ["make", "--no-print-directory", action, "DEPLOYMENT=" + self.name],
            "Stress " + action, environment=clean_environment(), timeout=660,
        )

    def compose_run(self, *arguments, timeout=90, capture=False):
        self.phase = "fixture " + arguments[0]
        return self.docker.run(*self.compose, *arguments, env=self.environment,
                               timeout=timeout, capture=capture)

    def container(self, service, *, running=False):
        ids = self.docker.run(
            "ps", "--all", "--no-trunc",
            "--filter", "label=com.docker.compose.project=" + self.project,
            "--filter", "label=com.docker.compose.service=" + service,
            "--format", "{{.ID}}", capture=True,
        ).splitlines()
        require(len(ids) == 1 and ID.fullmatch(ids[0]),
                "Capture service identity is missing or ambiguous.")
        record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}",
                                           ids[0], capture=True))
        require(record["Id"] == ids[0] and record["Config"]["Labels"].get(OWNER_LABEL) == self.owner
                and record["Config"]["Labels"].get("com.docker.compose.service") == service
                and record["Config"]["Image"] == (self.runner if service in ("gateway", "observer") else self.images["postgres"]),
                "Capture service changed immutable identity or owner.")
        if running:
            require(record["State"]["Running"] is True, "Capture service exited during the observation window.")
        return ids[0], record

    def preflight(self):
        require(not self.profile_dir.exists() and not self.profile_dir.is_symlink(),
                "Stress capture refuses a previous generated deployment.")
        require(not _candidate_ids(self.docker, "conix-" + self.name),
                "Stress capture refuses an existing retained process.")
        require(not self.docker.run("ps", "--all", "--filter",
                                   "label=com.docker.compose.project=" + self.project,
                                   "--format", "{{.ID}}", capture=True).strip(),
                "Stress fixture project is already present.")
        for resource, suffixes in (("network", ("tests", "rpc", "egress")),
                                   ("volume", ("pgdata", "application", "observer"))):
            for suffix in suffixes:
                name = self.project + "_" + suffix
                found = self.docker.run(resource, "ls", "--filter", "name=" + name,
                                        "--format", "{{.Name}}", capture=True).splitlines()
                require(name not in found, "Capture refuses an existing owned-resource name.")
        self.runner = SharedWheelhouse(self.docker, self.images, self.profile["wheelhouse"]).build("test-runner")
        self.docker.require_role(self.runner, "test-runner")
        self.docker.require_image(self.images["postgres"])
        self.environment["CONIX_TEST_RUNNER_IMAGE"] = self.runner
        self.records["test_image"] = self.runner
        # Render first; refuse unintended authority before any Compose effects.
        graph = json.loads(self.compose_run("config", "--format", "json", capture=True))
        self.admit_graph(graph)

    def admit_graph(self, graph):
        services = graph["services"]
        require(set(services) == {"postgres", "bootstrap", "archive-init", "gateway", "observer"}
                and set(graph["networks"]) == {"tests", "rpc", "egress"}
                and set(graph["volumes"]) == {"pgdata", "application", "observer"},
                "Stress capture fixture graph changed.")
        for name, service in services.items():
            expected = self.runner if name in ("gateway", "observer", "archive-init") else self.images["postgres"]
            networks = {"gateway": {"rpc", "egress"}, "observer": {"egress"},
                        "postgres": {"tests"}, "bootstrap": {"tests"}, "archive-init": set()}[name]
            require(service.get("image") == expected and service.get("pull_policy") == "never"
                    and service.get("read_only") is True
                    and set(service.get("networks", {})) == networks
                    and service.get("labels", {}).get(OWNER_LABEL) == self.owner
                    and not any(service.get(key) for key in ("ports", "privileged", "build", "pid", "devices", "secrets", "configs"))
                    and set(service.get("cap_drop", [])) == {"ALL"},
                    "Capture service exceeds its declared image/network grants.")
            mounts = service.get("volumes", [])
            expect_mounts = {
                "postgres": {("pgdata", "/var/lib/postgresql/data")},
                "bootstrap": set(), "gateway": {("application", "/capture")},
                "observer": {("observer", "/capture")},
                "archive-init": {("application", "/init/application"), ("observer", "/init/observer")},
            }[name]
            require({(m.get("source"), m.get("target")) for m in mounts} == expect_mounts
                    and all(m.get("type") == "volume" for m in mounts),
                    "Capture service has unselected filesystem authority.")
        for name in ("gateway", "observer"):
            service = services[name]
            require(service.get("user") == "65532:65532"
                    and service.get("security_opt") == ["no-new-privileges:true"]
                    and str(service.get("mem_limit")) in ("256m", "268435456")
                    and str(service.get("memswap_limit")) in ("256m", "268435456")
                    and service.get("pids_limit") == 64
                    and service.get("entrypoint") == ["python", "-E", "-s", "/tests/stress_" + name + ".py"],
                    "Capture worker confinement changed.")
        initializer = services["archive-init"]
        require(initializer.get("user") == "0:0" and set(initializer.get("cap_add", [])) == {"CHOWN"}
                and initializer.get("network_mode") == "none",
                "Archive volume initializer exceeded its narrow grant.")
        for name, network in graph["networks"].items():
            require(network.get("name") == self.project + "_" + name
                    and network.get("internal", False) == (name != "egress")
                    and network.get("labels", {}).get(OWNER_LABEL) == self.owner
                    and not network.get("external") and not network.get("driver_opts"),
                    "Capture fixture network is not selected.")
        for name, volume in graph["volumes"].items():
            require(volume.get("name") == self.project + "_" + name
                    and volume.get("labels", {}).get(OWNER_LABEL) == self.owner
                    and not volume.get("external") and not volume.get("driver_opts"),
                    "Capture volume is not owned by this fixture.")

    def start(self):
        self.fixture_attempted = True
        self.compose_run("run", "--rm", "--no-deps", "--pull", "never",
                         "--interactive=false", "-T", "archive-init", timeout=60)
        self.compose_run("up", "--detach", "--no-build", "--pull", "never",
                         "--wait", "--wait-timeout", "60", "postgres", "gateway", "observer", timeout=90)
        self.compose_run("run", "--rm", "--no-deps", "--pull", "never",
                         "--interactive=false", "-T", "bootstrap", timeout=90)
        self.container("gateway", running=True)
        self.container("observer", running=True)

    def control(self, service, command):
        identity, _ = self.container(service, running=True)
        return self.docker.run(
            "exec", "--user", "65532:65532", identity,
            "python", "-E", "-s", "/tests/stress_control.py", command,
            capture=True, timeout=15,
        ).strip()

    def seed(self):
        deadline = time.monotonic() + 45
        while True:
            try:
                raw = self.control("observer", "seed")
                seed = json.loads(raw)
                header = seed["header"]
                require(seed["status"] == "observed" and type(header["height"]) is int
                        and type(header["hash"]) is str and type(header["parent_hash"]) is str,
                        "Seed observation is malformed.")
                self.records["seed"] = seed
                return header["height"]
            except (OperationError, ValueError, KeyError, TypeError):
                self.container("observer", running=True)
                if time.monotonic() >= deadline:
                    raise OperationError("Independent recorded seed observation did not complete.")
                time.sleep(0.5)

    def create_profile(self, seed):
        self.phase = "dynamic stress deployment"
        self.profile_dir.mkdir(mode=0o700)
        self.profile_created = True
        for child in ("abi", "secrets"):
            (self.profile_dir / child).mkdir(mode=0o700)
        document = dict(self.profile)
        document["chain"] = {**document["chain"], "start_block": seed}
        document.update(database_network=self.project + "_tests", rpc_network=self.project + "_rpc")
        put_private(self.profile_dir / "deployment.toml", named.render_deployment(document).encode())
        put_private(self.profile_dir / "abi/pay_to_play.json", self.abi)
        for role in ("owner", "runtime", "reader"):
            credential = f"postgresql://conix_{role}:conix-disposable-{role}@postgres:5432/conix_rpc_test\n"
            put_private(self.profile_dir / "secrets" / (role + ".dsn"), credential.encode())
        put_private(self.folder / "source.json", self.source_raw)
        put_private(self.folder / "abi.json", self.abi)
        put_private(self.folder / "deployment.toml", (self.profile_dir / "deployment.toml").read_bytes())

    def calibrate(self, service):
        sent = time.monotonic_ns()
        response = self.control(service, "clock")
        received = time.monotonic_ns()
        require(response.isdecimal(), "Worker returned invalid monotonic clock evidence.")
        worker = int(response)
        lower, upper = worker - received, worker - sent
        require(upper >= lower and upper - lower <= self.selection["stress_clock_tolerance_ms"] * 1000000,
                "Worker clock exchange exceeded the explicit uncertainty tolerance.")
        return {"worker": worker, "sent": sent, "received": received,
                "lower_offset_ns": lower, "upper_offset_ns": upper}

    def clock_mapping(self, before, after):
        lower = max(before["lower_offset_ns"], after["lower_offset_ns"])
        upper = min(before["upper_offset_ns"], after["upper_offset_ns"])
        require(lower <= upper, "Worker monotonic clock mappings disagree across the window.")
        return {"before": before, "after": after, "offset_ns": (lower + upper) // 2,
                "uncertainty_ns": (upper - lower) // 2}

    def deployed(self):
        ids = _candidate_ids(self.docker, "conix-" + self.name)
        require(len(ids) == 1, "Stress requires one exact retained Conix process.")
        record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}",
                                           ids[0], capture=True))
        prepared = named.inspect_prepared(self.name)
        require(record["Id"] == ids[0] and record["State"]["Running"] is True
                and record["Image"] == prepared.image
                and record["Config"]["Cmd"] == ["-I", "-m", "conix", "run"]
                and record["Config"]["User"] == "65532:65532"
                and record["HostConfig"]["ReadonlyRootfs"] is True
                and set(record["NetworkSettings"]["Networks"]) == {
                    self.project + "_tests", self.project + "_rpc"},
                "Original Conix process identity or grants changed.")
        return ids[0]

    def history(self, identity, since, until):
        raw = self.docker.run(
            "events", "--since", since.isoformat(), "--until", until.isoformat(),
            "--filter", "container=" + identity, "--format", "{{json .}}",
            capture=True, timeout=30,
        )
        events = [json.loads(line) for line in raw.splitlines()]
        relevant = [event["Action"] for event in events
                    if event.get("Type") == "container" and event.get("Actor", {}).get("ID") == identity
                    and event.get("Action") in ("start", "die", "restart")]
        require(relevant and relevant[0] == "start" and relevant.count("start") == 1
                and "die" not in relevant and "restart" not in relevant,
                "Engine events do not confirm uninterrupted original Conix execution.")
        return relevant

    def observe(self):
        started_events = datetime.now(timezone.utc) - timedelta(seconds=2)
        self.make("deployment/prepare")
        self.prepared = True
        self.make("deployment/database/schema/init")
        self.make("deployment/config/check")
        self.make("deployment/up")
        self.launched = True
        identity = self.deployed()
        self.records["indexer_id"] = identity
        before = {service: self.calibrate(service) for service in ("gateway", "observer")}
        release_send = time.monotonic_ns()
        self.control("observer", "release")
        self.records["origin_ns"] = release_send
        self.records["released_utc"] = datetime.now(timezone.utc).isoformat()
        # The independent observer is one bounded running process, not a tape
        # advanced by application requests.
        deadline = time.monotonic() + self.selection["stress_duration_seconds"] + 40
        while True:
            self.deployed()
            self.container("observer", running=True)
            try:
                result = json.loads(self.control("observer", "done"))
                require(result["status"] in ("complete", "partial"),
                        "Observer returned an unknown final capture status.")
                break
            except OperationError:
                if time.monotonic() >= deadline:
                    raise OperationError("Independent observer exceeded its bounded capture budget.")
                time.sleep(0.5)
        after = {service: self.calibrate(service) for service in ("gateway", "observer")}
        self.records["clocks"] = {
            service: self.clock_mapping(before[service], after[service])
            for service in ("gateway", "observer")
        }
        finished_events = datetime.now(timezone.utc) + timedelta(seconds=1)
        require(self.deployed() == identity, "Retained Conix identity changed before the final head.")
        self.records["indexer_events"] = self.history(identity, started_events, finished_events)
        self.records["process_continuity"] = "confirmed"
        self.control("observer", "retire")
        retirement_deadline = time.monotonic() + 15
        while self.container("observer")[1]["State"]["Running"]:
            require(time.monotonic() < retirement_deadline,
                    "Observer worker retirement remained unconfirmed.")
            time.sleep(0.1)
        require(self.container("observer")[1]["State"]["ExitCode"] == 0,
                "Independent observer exited without a clean archive/receipt.")

    def stop(self):
        """Retire the original process before closing either independent recorder."""
        self.phase = "retirement before export"
        if self.profile_created:
            self.make("deployment/down")
            require(not _candidate_ids(self.docker, "conix-" + self.name),
                    "Original Conix retirement is unconfirmed.")
        self.consumer_retired = True
        first_failure = None
        if self.fixture_attempted:
            for name in ("gateway", "observer"):
                try:
                    identity, record = self.container(name)
                    if record["State"]["Running"]:
                        self.docker.run("container", "stop", "--time", "25", identity, timeout=35)
                        identity, record = self.container(name)
                    require(record["State"]["Running"] is False
                            and record["State"]["ExitCode"] == 0,
                            "Capture worker retirement did not finalize cleanly.")
                    self.retired.add(name)
                except BaseException as error:
                    first_failure = first_failure or error
        if first_failure is not None:
            raise first_failure

    def owned_volume(self, name):
        volume = self.project + "_" + name
        record = json.loads(self.docker.run("volume", "inspect", "--format", "{{json .}}", volume,
                                           capture=True))
        require(record["Name"] == volume and record["Driver"] == "local"
                and record["Labels"].get(OWNER_LABEL) == self.owner,
                "Capture volume ownership changed; preserve it.")
        return volume

    def export(self, name):
        """A networkless volume-only helper; Docker cp never prints WARC bytes."""
        require(name in self.retired, "Archive cannot be exported before its recorder retires cleanly.")
        volume = self.owned_volume(name)
        helper = "conix-export-" + name + "-" + self.owner
        self.phase = "export " + name
        self.docker.run(
            "create", "--name", helper, "--pull", "never",
            "--label", OWNER_LABEL + "=" + self.owner,
            "--network", "none", "--read-only", "--user", "65532:65532",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "16", "--memory", "128m",
            "--mount", f"type=volume,source={volume},target=/capture,readonly,volume-nocopy",
            "--entrypoint", "python", self.runner, "-I", "-c", "import time; time.sleep(60)",
            timeout=30, capture=True,
        )
        try:
            self.docker.run("container", "start", helper, timeout=15)
            archive = self.folder / (name + ".warc.gz")
            self.docker.run("cp", helper + ":/capture/" + name + ".warc.gz", str(archive), timeout=30)
            archive_hash = check_file(archive, self.selection["stress_archive_bytes"])
            for control in ("receipt.json",) + (("observations.json",) if name == "observer" else ()):
                path = self.folder / (name + "-" + control)
                self.docker.run("cp", helper + ":/capture/" + control, str(path), timeout=15)
                check_file(path, 1048576)
            # A different process reads raw bytes using warcio without importing
            # the recorder, writer, scanner, database or a provider endpoint.
            raw = self.docker.run(
                "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                "--user", "65532:65532", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                "--pids-limit", "32", "--memory", "256m",
                "--mount", f"type=volume,source={volume},target=/capture,readonly,volume-nocopy",
                "--entrypoint", "python", self.runner,
                "-E", "-s", "/tests/stress_inspect.py", name, capture=True, timeout=60,
            )
            inspected = json.loads(raw)
            require(inspected["status"] == "verified" and inspected["stream"] == name,
                    "Independent raw WARC inspection is unavailable.")
            self.archive_exported.add(name)
            self.records["export"][name] = {
                "status": "verified", "sha256": archive_hash, "inspection": inspected,
            }
        finally:
            ids = self.docker.run("ps", "--all", "--no-trunc", "--filter",
                                  "name=^/" + helper + "$", "--format", "{{.ID}}", capture=True).splitlines()
            for identity in ids:
                record = json.loads(self.docker.run("container", "inspect", "--format",
                                                   "{{json .}}", identity, capture=True))
                require(record["Id"] == identity and record["Config"]["Labels"].get(OWNER_LABEL) == self.owner,
                        "Export helper retirement ownership is unconfirmed.")
                self.docker.run("container", "rm", "--force", identity, timeout=30)

    def complete(self):
        observer = json.loads((self.folder / "observer-observations.json").read_text())
        gateway = json.loads((self.folder / "application-receipt.json").read_text())
        observed_receipt = json.loads((self.folder / "observer-receipt.json").read_text())
        seed = observer["seed"]
        require(seed == self.records["seed"]
                and observer["admission"]["status"] == "complete"
                and gateway["status"] == observed_receipt["status"] == "complete",
                "Recorder completion or observation-window admission is unavailable.")
        first, last = observer["samples"][0], observer["samples"][-1]
        require(first["status"] == last["status"] == "observed"
                and last["header"]["height"] > first["header"]["height"],
                "Fuji observed head did not advance through a complete window.")
        inspection_seed = self.records["export"]["observer"]["inspection"]["first_header"]
        require(inspection_seed["height"] == seed["header"]["height"]
                and inspection_seed["hash"] == seed["header"]["hash"]
                and inspection_seed["parent_hash"] == seed["header"]["parent_hash"],
                "Raw WARC seed body conflicts with observer control record.")
        require(self.records.get("process_continuity") == "confirmed",
                "Original retained process continuity is unconfirmed.")
        for sample in observer["samples"]:
            offset = self.records["clocks"]["observer"]["offset_ns"]
            sample["elapsed_start_ns"] = sample["started_ns"] - offset - self.records["origin_ns"]
            sample["elapsed_end_ns"] = sample["finished_ns"] - offset - self.records["origin_ns"]
        self.records.update({
            "capture": "complete", "observer": observer,
            "gateway_receipt": gateway, "observer_receipt": observed_receipt,
            "initial_head_advance": first["header"]["height"] - seed["header"]["height"],
            "initial_pending_without_checkpoint": first["header"]["height"] - seed["header"]["height"] + 1,
        })

    def verify_fixture_ownership(self):
        """Every existing resource must belong to this run before Compose down."""
        identities = self.docker.run(
            "ps", "--all", "--no-trunc",
            "--filter", "label=com.docker.compose.project=" + self.project,
            "--format", "{{.ID}}", capture=True,
        ).splitlines()
        for identity in identities:
            require(ID.fullmatch(identity), "Fixture returned an incomplete container ID.")
            record = json.loads(self.docker.run(
                "container", "inspect", "--format", "{{json .}}", identity, capture=True,
            ))
            labels = record["Config"]["Labels"]
            service = labels.get("com.docker.compose.service")
            require(record["Id"] == identity and labels.get(OWNER_LABEL) == self.owner
                    and service in ("postgres", "bootstrap", "archive-init", "gateway", "observer"),
                    "Fixture container ownership changed; preserve its dependencies.")
        for kind, names in (("network", ("tests", "rpc", "egress")),
                            ("volume", ("pgdata", "application", "observer"))):
            for name in names:
                actual = self.project + "_" + name
                existing = self.docker.run(kind, "ls", "--filter", "name=" + actual,
                                           "--format", "{{.Name}}", capture=True).splitlines()
                if actual not in existing:
                    continue
                record = json.loads(self.docker.run(kind, "inspect", actual, capture=True))[0]
                require(record["Name"] == actual and record["Labels"].get(OWNER_LABEL) == self.owner,
                        "Fixture network/volume owner changed; preserve it.")

    def cleanup(self):
        """Do not retire dependencies under an unconfirmed original process."""
        failures = []
        if self.fixture_attempted:
            try:
                if not self.consumer_retired:
                    if self.profile_created:
                        self.make("deployment/down")
                        require(not _candidate_ids(self.docker, "conix-" + self.name),
                                "Original Conix still owns fixture dependencies.")
                    self.consumer_retired = True
                for service in ("gateway", "observer"):
                    ids = self.docker.run(
                        "ps", "--all", "--no-trunc",
                        "--filter", "label=com.docker.compose.project=" + self.project,
                        "--filter", "label=com.docker.compose.service=" + service,
                        "--format", "{{.ID}}", capture=True,
                    ).splitlines()
                    if not ids:
                        continue
                    identity, record = self.container(service)
                    if record["State"]["Running"]:
                        self.docker.run("container", "stop", "--time", "25", identity, timeout=35)
                self.verify_fixture_ownership()
                self.compose_run("down", "--remove-orphans", "--timeout", "30", timeout=90)
                for name in ("pgdata", "application", "observer"):
                    if name in ("application", "observer") and name not in self.archive_exported:
                        # No confirmed independent export: keep the only volume evidence.
                        continue
                    volume = self.owned_volume(name)
                    self.docker.run("volume", "rm", volume, timeout=30)
            except BaseException as error:
                failures.append("fixture: " + type(error).__name__)
        if self.profile_created and not failures and self.consumer_retired:
            try:
                prepared = named.inspect_prepared(self.name)
                if prepared is not None:
                    self.docker.remove_inputs(prepared.volume)
                    named.clear_prepared(self.name, expected_volume=prepared.volume)
                shutil.rmtree(self.profile_dir)
            except BaseException as error:
                failures.append("generated deployment: " + type(error).__name__)
        self.records["cleanup"] = failures
        return failures


def run_capture(options, *, client_cls, pinned_images):
    """Maintained operator: one bounded live capture and independently verified export."""
    require(os.geteuid() != 0, "Stress capture requires an ordinary non-root operator.")
    selection = supplied(options)
    source, profile, _, _ = selected_source()
    require(not options.context or options.context == profile["docker_context"],
            "Use the reviewed Fuji profile's selected Docker Engine.")
    require(not options.wheelhouse or options.wheelhouse == profile["wheelhouse"],
            "Use the reviewed Fuji profile's selected wheelhouse.")
    root = ROOT / "out/rpc-recordings"
    with campaign_lock(root), cancellation():
        endpoint = select_endpoint(profile["docker_context"], "command line", working_dir=ROOT)
        import tempfile
        with tempfile.TemporaryDirectory(prefix="conix-stress-client-") as directory:
            config = Path(directory)
            put_private(config / "config.json", b'{"auths":{},"proxies":{}}\n')
            docker = client_cls(endpoint, config)
            docker.require_rootless_engine()
            capture = StressCapture(docker, pinned_images(), options, root, selection)
            primary = None
            try:
                capture.preflight()
                capture.start()
                seed = capture.seed()
                capture.create_profile(seed)
                capture.observe()
            except BaseException as error:
                primary = error
            try:
                capture.stop()
            except BaseException as error:
                primary = primary or error
            # Retain partial archives even when the observer or Conix failed.
            if capture.fixture_attempted and capture.runner is not None:
                for name in ("application", "observer"):
                    if name not in capture.retired:
                        capture.records["export"][name] = {"status": "unconfirmed", "failure": "retirement"}
                        continue
                    try:
                        capture.export(name)
                    except BaseException as error:
                        capture.records["export"][name] = {"status": "unconfirmed", "failure": type(error).__name__}
                        primary = primary or error
            if primary is None:
                try:
                    capture.complete()
                except BaseException as error:
                    primary = error
            failures = capture.cleanup()
            if primary is not None:
                capture.records["capture"] = "partial"
                capture.records["primary_failure"] = type(primary).__name__
            if failures:
                capture.records["cleanup"] = failures
            try:
                json_record(capture.folder / "manifest.json", capture.records)
            except BaseException as error:
                primary = primary or error
            progress("Stress capture " + capture.records["capture"] + "; private receipt: " +
                     str(capture.folder / "manifest.json"))
            if primary is not None or failures:
                raise OperationError("Stress capture or its owned cleanup is incomplete; retained evidence and recovery record identify the selected resources.")
