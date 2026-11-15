"""Own bounded read-only Fuji acceptance; use the ordinary named lifecycle.

The historical profile is committed. The recent profile is created once per
owned run with its own start/database and retained only if recovery is needed.
No chain mutations, sibling source imports or alternate scanner execute here.
"""

from copy import deepcopy
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
import tomllib
import traceback

from tools.containers import named
from tools.containers.client import OperationError, select_endpoint
from tools.containers.deployment import _candidate_ids
from tools.containers.diagnostics import PREFIX, PHASES, error_record, explanation
from tools.containers.resources import _admit_volume, _admit_network
from tools.fixtures.deployment import campaign_lock
from tools.fixtures.process import CampaignError, cancellation, clean_environment, owned_command, progress
from tools.wheelhouse.operations import SharedWheelhouse

NAME = "fuji-acceptance"
TIP = "fuji-tip-acceptance"
PROJECT = "conix-fuji-acceptance-fixture"
OWNER_LABEL = "io.conix.acceptance-owner"
HELPER_LABEL = "io.conix.fixture=fuji-acceptance"
SOURCE_FILE = named.ROOT / "tests/fixtures/evm/fuji_pay_to_play.json"


def require(condition, message):
    if not condition:
        raise OperationError(message)


def admit_fixture(graph, images, runner, owner, view):
    """Only the gateway receives external networking; no service receives host access."""
    try:
        require(set(graph["services"]) == {"postgres", "bootstrap", "gateway"}
                and set(graph["networks"]) == {"tests", "rpc", "egress"}
                and set(graph["volumes"]) == {"pgdata"}, "Fuji fixture resource graph differs from its reviewed selection.")
        for name, service in graph["services"].items():
            expected = runner if name == "gateway" else images["postgres"]
            peers = {"rpc", "egress"} if name == "gateway" else {"tests"}
            require(service.get("image") == expected and service.get("pull_policy") == "never"
                    and service.get("read_only") is True and set(service.get("networks", {})) == peers
                    and service.get("user") == ("65532:65532" if name == "gateway" else "999:999")
                    and set(service.get("cap_drop", [])) == {"ALL"}
                    and service.get("security_opt") == ["no-new-privileges:true"]
                    and service.get("labels", {}).get(OWNER_LABEL) == owner
                    and all(m.get("type") == "volume" for m in service.get("volumes", []))
                    and not any(service.get(key) for key in ("privileged", "ports", "build", "network_mode", "cap_add", "devices", "pid")),
                    "Fuji fixture service exceeds its image, identity or network grants.")
        for name, service in graph["services"].items():
            mounts = service.get("volumes", [])
            if name == "postgres":
                require(len(mounts) == 1 and mounts[0].get("type") == "volume"
                        and mounts[0].get("source") == "pgdata"
                        and mounts[0].get("target") == "/var/lib/postgresql/data"
                        and mounts[0].get("read_only", False) is False,
                        "Fuji PostgreSQL requires only its exact disposable data mount.")
            else:
                require(not mounts, "Fuji gateway/bootstrap receive no volume authority.")
            require(not service.get("secrets") and not service.get("configs"),
                    "Fuji fixture services receive no additional secret/config mounts.")
        require(graph["services"]["gateway"]["entrypoint"] == ["python", "-E", "-s", "/tests/fuji_rpc.py"]
                and graph["services"]["gateway"]["environment"] == {"CONIX_FUJI_VIEW": view},
                "Fuji gateway source view or entrypoint differs from the selected policy.")
        for name, network in graph["networks"].items():
            require(network.get("name") == PROJECT + "_" + name
                    and network.get("driver", "bridge") == "bridge"
                    and network.get("internal", False) == (name != "egress") and not network.get("external")
                    and not network.get("driver_opts") and network.get("labels", {}).get(OWNER_LABEL) == owner,
                    "Fuji fixture networks exceed the reviewed grants.")
        volume = graph["volumes"]["pgdata"]
        require(volume.get("name") == PROJECT + "_pgdata" and volume.get("driver", "local") == "local"
                and not volume.get("external") and not volume.get("driver_opts")
                and volume.get("labels", {}).get(OWNER_LABEL) == owner,
                "Fuji fixture database volume is not exclusively test-owned.")
    except (KeyError, TypeError, AttributeError) as error:
        raise OperationError("Cannot admit the rendered Fuji fixture graph.") from error


class FujiAcceptance:
    """Coordinate two explicit sources; ownership stays with each existing operator."""

    def __init__(self, docker, images, evidence, definition):
        (self.docker, self.images) = (docker, images)
        self.definition = definition
        self.source = json.loads(SOURCE_FILE.read_text())
        self.owner = secrets.token_hex(12)
        self.evidence = evidence / self.owner
        self.evidence.mkdir(mode=0o700)
        self.environment = docker.docker_environment.copy()
        self.environment.update(CONIX_ACCEPTANCE_OWNER=self.owner, CONIX_FUJI_VIEW="historical")
        self.compose = docker.docker_command_prefix + ["compose", "--env-file", "/dev/null",
            "--project-directory", str(named.ROOT), "-p", PROJECT, "-f", str(named.ROOT / "compose/fuji-test.yml")]
        self.step = 0
        self.fixture_attempted = False
        self.tip_created = False
        self.tip_attempted = False
        self.touched = []
        self.runner = None
        self.records = {"revision": "", "endpoint": docker.endpoint, "owner": self.owner, "checks": [], "phase": "preflight"}

    def child(self, argv, label, *, payload=None, environment=None, timeout=90):
        self.step += 1
        self.records["phase"] = label
        prefix = self.evidence / f"{self.step:03d}"
        return owned_command(argv, output=prefix.with_suffix(".out"), error_file=prefix.with_suffix(".err"),
            env=self.environment if environment is None else environment,
            timeout=timeout, payload=payload, label=label)

    def make(self, action, name=NAME):
        require(select_endpoint(self.definition["docker_context"], "command line", working_dir=named.ROOT) == self.docker.endpoint,
                "Selected Fuji Engine changed during acceptance.")
        argv = ["make", "--no-print-directory", action, "DEPLOYMENT=" + name]
        if action == "deployment/config/init":
            argv.append("DOCKER_CONTEXT=" + self.definition["docker_context"])
        try:
            return self.child(argv, name + " " + action, environment=clean_environment(), timeout=660)
        except CampaignError as error:
            try:
                with (self.evidence / f"{self.step:03d}.err").open("rb") as stream:
                    stderr = stream.read(65537)
            except OSError:
                raise error
            records = [line for line in stderr.splitlines() if line.startswith(PREFIX)]
            record = records[0] if len(stderr) <= 65536 and len(records) == 1 else None
            detail = explanation(record, error.exit_code, phases=PHASES)
            if detail is not None:
                raise OperationError(f"{name} {action} failed: {detail}", diagnostic=record) from error
            raise

    def preflight(self):
        """Refuse prior state instead of adopting or retiring another run's resources."""
        require(not (named.ROOT / "config/deployments" / TIP).exists(), "A recent Fuji deployment already exists; inspect its owner first.")
        require(named.inspect_prepared(NAME) is None and named.inspect_reader_prepared(NAME) is None,
                "Fuji acceptance found a prior prepared selection; inspect it first.")
        for name in (NAME, TIP):
            require(not _candidate_ids(self.docker, "conix-" + name), "Fuji acceptance found an existing named process.")
        for resource, names in (("network", [PROJECT + suffix for suffix in ("_tests", "_rpc", "_egress")]),
                                ("volume", [PROJECT + "_pgdata"])):
            for name in names:
                found = self.docker.run(resource, "ls", "--filter", "name=" + name, "--format", "{{.Name}}", capture=True).splitlines()
                require(name not in found, "Fuji fixture resources already exist; inspect their owner first.")
        require(not self.docker.run("ps", "--all", "--filter", "label=com.docker.compose.project=" + PROJECT,
                    "--format", "{{.ID}}", capture=True).strip(), "Fuji fixture containers already exist.")
        require(not self.docker.run("ps", "--all", "--filter", "label=" + HELPER_LABEL,
                    "--format", "{{.ID}}", capture=True).strip(), "A Fuji helper remains; inspect its owner first.")
        raw = (named.ROOT / "config/deployments" / NAME / "abi/pay_to_play.json").read_bytes()
        require(hashlib.sha256(raw).hexdigest() == self.source["abi_sha256"], "Fuji ABI differs from the pinned deployment artifact.")
        selected = self.definition["contracts"]["payment"]
        signatures = {e["name"] + "(" + ",".join(a["type"] for a in e["inputs"]) + ")"
                      for e in json.loads(raw) if e.get("type") == "event"}
        require(set(selected["events"]) == signatures and len(signatures) == 17
                and selected["address"].lower() == self.source["address"]
                and self.definition["chain"]["chain_id"] == self.source["chain_id"]
                and self.definition["chain"]["genesis_hash"] == self.source["genesis_hash"]
                and self.definition["chain"]["start_block"] == self.source["initial_interval"]["start_block"],
                "Fuji profile no longer selects the complete reviewed deployment source.")
        require(self.definition["database_network"] == PROJECT + "_tests"
                and self.definition["rpc_network"] == PROJECT + "_rpc", "Fuji profile must select its dedicated test networks.")

    def start_fixture(self):
        """Use reviewed existing image roles and explicit fixture creation boundaries."""
        self.runner = SharedWheelhouse(self.docker, self.images, self.definition["wheelhouse"]).build("test-runner")
        self.docker.require_role(self.runner, "test-runner")
        self.docker.require_image(self.images["postgres"])
        self.environment.update(CONIX_TEST_RUNNER_IMAGE=self.runner, CONIX_POSTGRES_IMAGE=self.images["postgres"],
                                CONIX_FOUNDRY_IMAGE=self.images["foundry"])
        self.admit_graph()
        self.fixture_attempted = True
        self.child(self.compose + ["up", "--detach", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60",
                                  "postgres", "gateway"], "Fuji fixture startup")
        self.child(self.compose + ["run", "--rm", "--no-deps", "--pull", "never", "--interactive=false", "-T", "bootstrap"],
                   "Fuji database roles")

    def admit_graph(self):
        graph = json.loads(self.child(self.compose + ["config", "--format", "json"], "Fuji fixture admission"))
        admit_fixture(graph, self.images, self.runner, self.owner, self.environment["CONIX_FUJI_VIEW"])

    def probe(self, operation, name=NAME):
        """Only prepared, immutable volumes and stdin credentials reach the helper."""
        request = {"operation": operation}
        for network in (self.definition["rpc_network"], self.definition["database_network"]):
            _admit_network(self.docker, network)
        argv = self.docker.docker_command_prefix + ["run", "--rm", "--interactive", "--pull", "never",
            "--label", HELPER_LABEL, "--label", OWNER_LABEL + "=" + self.owner,
            "--network", self.definition["rpc_network"], "--network", self.definition["database_network"],
            "--read-only", "--user", "65532:65532", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "256m", "--memory-swap", "256m",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,uid=65532,gid=65532,mode=0700,size=8m"]
        if operation in ("snapshot", "reader"):
            credential = named.ROOT / "config/deployments" / name / "secrets/reader.dsn"
            request["dsn"] = self.docker.private_bytes(credential, 8192, secret=True).decode().strip()
        if operation == "reader":
            prepared = named.inspect_reader_prepared(name)
            require(prepared is not None, "Fuji Reader snapshot is absent.")
            self.docker.require_role(prepared.image, "indexer")
            _admit_volume(self.docker, prepared.volume)
            self.docker.verify_inputs(prepared.image, prepared.volume)
            argv += ["--mount", f"type=volume,source={prepared.volume},target=/run/conix,readonly,volume-nocopy"]
        argv += ["--entrypoint", "python", self.runner, "-E", "-s", "/tests/fuji_probe.py"]
        try:
            result = json.loads(self.child(argv, "Fuji " + operation, payload=json.dumps(request).encode(), timeout=90))
        except CampaignError as error:
            try:
                with (self.evidence / f"{self.step:03d}.err").open("rb") as stream:
                    diagnostic = stream.read(1025)
            except OSError:
                raise error
            match = re.fullmatch(
                rb"Fuji probe failed \([A-Za-z][A-Za-z0-9_]{0,63}\) at [A-Za-z0-9_]{1,64}\.py:[1-9][0-9]{0,5}\."
                rb"(?:(?: (?:HTTP|RPC) eth_[A-Za-z0-9]{1,40} code (?:-?[0-9]{1,10}|unavailable)\."
                rb"(?: Upstream [A-Za-z][A-Za-z0-9_]{0,63}(?: status [1-5][0-9]{2})?\.)?)|(?: Check ([a-z_]{1,32})\.))?\n?", diagnostic,
            )
            if match:
                check = match[1].decode("ascii") if match[1] is not None else None
                if check is None or check in self.source["checks"]:
                    detail = diagnostic.decode("ascii").strip()
                    if check is not None:
                        detail += " " + self.source["checks"][check]
                    raise OperationError(detail) from error
            raise
        require(type(result) is dict, "Fuji observation is not a record.")
        return result

    def prepare(self, name):
        self.touched.append(name)
        for action in ("deployment/prepare", "deployment/database/schema/init", "deployment/config/check", "deployment/reader/prepare"):
            self.make(action, name)

    def running(self, name):
        identities = _candidate_ids(self.docker, "conix-" + name)
        require(len(identities) == 1, "Fuji acceptance requires one running retained process.")
        identity = identities[0]
        record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
        require(set(record["NetworkSettings"]["Networks"]) == {self.definition["database_network"], self.definition["rpc_network"]}
                and record["HostConfig"]["ReadonlyRootfs"] and not record["HostConfig"]["Privileged"]
                and record["Config"]["User"] == "65532:65532", "Fuji retained runtime grants changed.")
        require(record["Id"] == identity and record["State"]["Running"]
                and record["Image"] == named.inspect_prepared(name).image
                and record["Config"]["Cmd"] == ["-I", "-m", "conix", "run"], "Fuji retained process exited or changed identity.")
        return identity

    def reach(self, name, target, maximum_blocks=64):
        """Observe genuine progress under a budget; never replace a failed scanner."""
        deadline = time.monotonic() + 240
        while True:
            identity = self.running(name)
            state = self.probe("snapshot", name)
            require(state["blocked"] is None, "Fuji retained history became blocked.")
            require(len(state["blocks"]) <= maximum_blocks, "Fuji replay exceeded its declared block budget.")
            if state["height"] is not None and state["height"] >= target:
                return identity, state
            require(time.monotonic() < deadline, "Fuji source did not reach the target within the acceptance deadline.")
            time.sleep(0.5)

    def stop(self, name, identity):
        """Normal down must emit a confirmed zero-exit event before resource cleanup."""
        since = str(int(time.time()) - 1)
        self.make("deployment/down", name)
        events = self.docker.run("events", "--since", since, "--until", str(int(time.time()) + 1), "--filter",
            "container=" + identity, "--filter", "event=die", "--format", "{{json .}}", capture=True)
        require([json.loads(line)["Actor"]["Attributes"].get("exitCode") for line in events.splitlines()] == ["0"],
                "Fuji retained shutdown did not confirm a zero exit.")

    def historical(self):
        self.records["source"] = self.probe("source")
        self.records["gateway"] = self.probe("deny-write")
        self.records["network_policy"] = self.probe("network-policy")
        self.prepare(NAME)
        self.make("deployment/up")
        identity, state = self.reach(NAME, self.source["initial_interval"]["end_block"], maximum_blocks=1)
        observed = self.probe("reader")
        require(observed["height"] == self.source["initial_interval"]["end_block"],
                "Historical Reader checkpoint height differs from the selected interval.")
        require(observed["block_hash"] == self.source["initial_interval"]["block_hash"],
                "Historical Reader block identity differs from the independent receipt.")
        require(observed["events"] == self.source["expected_raw_logs"],
                "Historical Reader raw events differ from the independent creation receipt.")
        require(observed["asset"] == self.source["asset"], "Historical Reader asset differs from the pinned contract result.")
        require(set(observed["position"]) == set(self.source["position"]),
                "Historical Reader position fields differ from the pinned ABI return shape.")
        for field, expected in self.source["position"].items():
            require(observed["position"][field] == expected,
                    f"Historical Reader position field {field} differs from the pinned contract result.")
        self.make("deployment/reader/check")
        self.stop(NAME, identity)
        self.records["historical"] = {"view": "bounded_at_creation", "snapshot": state, "reader": observed}

    def create_recent(self, head):
        """Derive an ordinary named snapshot; never change an initialized source."""
        folder = named.ROOT / "config/deployments" / TIP
        require(not folder.exists(), "Recent Fuji profile already exists.")
        self.tip_attempted = True
        self.make("deployment/config/init", TIP)
        self.tip_created = True
        definition = deepcopy(self.definition)
        definition["chain"]["start_block"] = head
        destination = folder / "deployment.toml"
        destination.write_text(named.render_deployment(definition))
        destination.chmod(0o600)
        raw = (named.ROOT / "config/deployments" / NAME / "abi/pay_to_play.json").read_bytes()
        (folder / "abi/pay_to_play.json").write_bytes(raw)
        (folder / "abi/pay_to_play.json").chmod(0o600)
        for role in ("owner", "runtime", "reader"):
            path = folder / "secrets" / f"{role}.dsn"
            path.write_text(f"postgresql://conix_{role}:conix-disposable-{role}@postgres:5432/conix_rpc_test\n")
            path.chmod(0o600)
        (self.evidence / "recent-deployment.toml").write_text(destination.read_text())
        (self.evidence / "recent-deployment.toml").chmod(0o600)

    def recent(self):
        self.environment["CONIX_FUJI_VIEW"] = "live"
        self.admit_graph()
        self.child(self.compose + ["up", "--detach", "--no-build", "--pull", "never", "--force-recreate", "--wait",
                                  "--wait-timeout", "60", "gateway"], "genuine Fuji head gateway")
        start = self.probe("head")["height"]
        require(type(start) is int and start > self.source["initial_interval"]["end_block"], "Fuji source did not provide a recent head.")
        self.create_recent(start)
        self.prepare(TIP)
        self.make("deployment/up", TIP)
        head_at_start = self.probe("head")["height"]
        require(head_at_start - start < 60, "Fuji preparation lag exceeds the recent-window request budget.")
        first_id, first = self.reach(TIP, head_at_start + 1)
        self.stop(TIP, first_id)
        before = self.probe("snapshot", TIP)
        require(before["height"] >= start + 1, "Recent Fuji progress was not retained.")
        self.make("deployment/up", TIP)
        second_id, resumed = self.reach(TIP, before["height"] + 1)
        require(second_id != first_id and resumed["source_id"] == before["source_id"]
                and resumed["generation"] == before["generation"]
                and resumed["blocks"][:len(before["blocks"])] == before["blocks"]
                and resumed["events"][:len(before["events"])] == before["events"],
                "Fuji restart changed durable source or history.")
        self.stop(TIP, second_id)
        final = self.probe("snapshot", TIP)
        require(len(final["blocks"]) <= 64, "Fuji final stored interval exceeded its declared budget.")
        reader = self.probe("reader", TIP)
        self.make("deployment/reader/check", TIP)
        head = self.probe("head")["height"]
        self.records["recent"] = {"start": start, "head_at_start": head_at_start, "before_restart": before, "after_restart": resumed,
                                  "final": final, "reader": reader, "observed_head": head, "lag_blocks": head - reader["height"]}

    def verify_fixture_owner(self):
        """No project-name match alone authorizes resource deletion."""
        ids = self.docker.run("ps", "--all", "--no-trunc", "--filter", "label=com.docker.compose.project=" + PROJECT,
                              "--format", "{{.ID}}", capture=True).splitlines()
        for identity in ids:
            record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
            service = record["Config"]["Labels"].get("com.docker.compose.service")
            expected = self.runner if service == "gateway" else self.images["postgres"]
            require(service in ("gateway", "postgres", "bootstrap") and record["Id"] == identity
                    and record["Config"]["Labels"].get(OWNER_LABEL) == self.owner and record["Config"]["Image"] == expected,
                    "Fuji fixture container ownership changed; preserve it for inspection.")
        for resource, names in (("network", [PROJECT + suffix for suffix in ("_tests", "_rpc", "_egress")]),
                                ("volume", [PROJECT + "_pgdata"])):
            for name in names:
                found = self.docker.run(resource, "ls", "--filter", "name=" + name, "--format", "{{.Name}}", capture=True).splitlines()
                if name in found:
                    record = json.loads(self.docker.run(resource, "inspect", name, capture=True))[0]
                    require(record["Name"] == name and record["Labels"].get(OWNER_LABEL) == self.owner,
                            "Fuji fixture network/volume ownership changed.")

    def cleanup(self):
        """Keep dependencies and recovery records if any consumer retirement is uncertain."""
        failures = []
        if not self.fixture_attempted:
            return failures
        for name in reversed(self.touched):
            try:
                self.make("deployment/down", name)
                require(not _candidate_ids(self.docker, "conix-" + name), "Fuji consumer retirement is unconfirmed.")
                if named.inspect_reader_prepared(name) is not None:
                    self.make("deployment/reader/remove", name)
                self.records["phase"] = "deployment input retirement"
                prepared = named.inspect_prepared(name)
                if prepared is not None:
                    self.docker.remove_inputs(prepared.volume)
                    named.clear_prepared(name, expected_volume=prepared.volume)
            except BaseException as error:
                return [safe_failure(error, name + " " + self.records.get("phase", "consumer/input retirement"))]
        try:
            self.records["phase"] = "Fuji helper retirement"
            helpers = self.docker.run("ps", "--all", "--no-trunc", "--filter", "label=" + HELPER_LABEL,
                                      "--format", "{{.ID}}", capture=True).splitlines()
            for identity in helpers:
                record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
                require(record["Id"] == identity and record["Config"]["Labels"].get(OWNER_LABEL) == self.owner,
                        "Fuji helper ownership differs from this run.")
                self.docker.run("container", "rm", "--force", identity, capture=True)
            self.records["phase"] = "Fuji fixture ownership verification"
            self.verify_fixture_owner()
            self.child(self.compose + ["down", "--volumes", "--remove-orphans", "--timeout", "30"], "Fuji fixture retirement")
            for resource, action in (("ps", "--all"), ("network", "ls"), ("volume", "ls")):
                remaining = self.docker.run(resource, action, "--filter", "label=com.docker.compose.project=" + PROJECT,
                    "--format", "{{.Name}}" if resource == "volume" else "{{.ID}}", capture=True)
                require(not remaining.strip(), "Fuji fixture retirement left resources.")
            self.records["phase"] = "recent Fuji profile retirement"
            if self.tip_created:
                shutil.rmtree(named.ROOT / "config/deployments" / TIP)
        except BaseException as error:
            failures.append(safe_failure(error, self.records.get("phase", "fixture retirement")))
        if self.tip_attempted and not self.tip_created and (named.ROOT / "config/deployments" / TIP).exists():
            failures.append("recent Fuji profile creation was not confirmed; preserved for inspection")
        return failures


def safe_failure(error, phase):
    """Describe an unconfirmed owner operation without copying exception prose."""
    detail = explanation(error_record(error), getattr(error, "exit_code", None), phases=PHASES)
    return phase + ": " + detail


def publish_result(evidence, records):
    """Publish one private receipt atomically, preserving earlier evidence on failure."""
    descriptor, filename = tempfile.mkstemp(prefix="result-", suffix=".json", dir=evidence)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(records, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, evidence / "result.json")
    except BaseException as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            error.add_note("Fuji temporary receipt retirement is unconfirmed in the private evidence directory.")
        raise


def finish_acceptance(campaign, primary, failed_phase):
    """Cleanup and reporting have separate outcomes; the first failure remains primary."""
    try:
        failures = campaign.cleanup()
    except BaseException as error:
        failures = [safe_failure(error, "Fuji cleanup")]
        if primary is None:
            primary, failed_phase = error, "cleanup"
    record = None
    if primary is not None:
        category = type(primary).__name__
        category = category if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", category) else "UnexpectedError"
        frames = traceback.extract_tb(primary.__traceback__)
        location = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "unavailable"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}\.py:[1-9][0-9]{0,5}", location) is None:
            location = "unavailable"
        record = {"phase": failed_phase, "type": category, "location": location}
    campaign.records.update(passed=primary is None and not failures, cleanup_errors=failures, primary_failure=record)
    report_error = None
    try:
        publish_result(campaign.evidence, campaign.records)
    except BaseException as error:
        report_error = error
        campaign.records["passed"] = False
    messages = ["Cleanup unconfirmed: " + failure for failure in failures]
    if report_error is not None:
        messages.append("Fuji evidence publication failed (" + type(report_error).__name__ + "); current receipt is unconfirmed.")
    for message in messages:
        try:
            progress(message)
        except OSError:
            pass  # A closed terminal cannot replace the first operation failure.
    if primary is not None:
        if type(primary) in (OperationError, CampaignError):
            raise OperationError(str(primary), exit_code=primary.exit_code,
                                 diagnostic=getattr(primary, "diagnostic", None)) from primary
        if isinstance(primary, KeyboardInterrupt):
            raise primary
        raise OperationError(f"Fuji acceptance {failed_phase} failed ({category}) at {location}.") from primary
    require(not failures, "Fuji acceptance completed but cleanup is unconfirmed.")
    if report_error is not None:
        raise OperationError("Fuji checks and cleanup completed, but evidence publication failed; current receipt is unconfirmed.") from report_error
    try:
        progress("PASS: read-only Fuji historical and recent deployment acceptance; evidence: " + str(campaign.evidence / "result.json"))
    except OSError as error:
        raise OperationError("Fuji checks, cleanup and evidence publication completed, but terminal reporting failed.") from error


def run_acceptance(options, *, client_cls, pinned_images):
    """Run one live external lane with bounded diagnostics and separate cleanup truth."""
    require(not options.deployment or options.deployment == NAME, "Fuji acceptance owns DEPLOYMENT=fuji-acceptance only.")
    require(os.geteuid() != 0, "Fuji acceptance must run as the ordinary host operator.")
    folder = named.ROOT / "config/deployments" / NAME
    for directory in (folder, folder / "abi", folder / "secrets"):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(), "Fuji profile requires ordinary owned private directories.")
        directory.chmod(0o700)
    for path in (folder / "deployment.toml", folder / "abi/pay_to_play.json", *(folder / "secrets" / (r + ".dsn") for r in ("owner", "runtime", "reader"))):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(), "Fuji inputs must be ordinary owned files.")
        path.chmod(0o600)
    definition = tomllib.loads((folder / "deployment.toml").read_text())
    require(not options.context or options.context == definition["docker_context"], "Use the Fuji profile's selected Docker context.")
    require(not options.wheelhouse or options.wheelhouse == definition["wheelhouse"], "Use the Fuji profile's selected wheelhouse.")
    named.inspect_named(NAME)
    evidence = named.ROOT / "out/fuji-acceptance"
    with campaign_lock(evidence), cancellation():
        endpoint = select_endpoint(definition["docker_context"], "command line", working_dir=named.ROOT)
        client = evidence / "docker-client"
        client.mkdir(mode=0o700, exist_ok=True)
        info = client.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                "Fuji Docker-client evidence directory is not privately owned.")
        config = client / "config.json"
        expected = b'{"auths":{},"proxies":{}}\n'
        if config.exists() or config.is_symlink():
            info = config.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077
                    and config.read_bytes() == expected, "Fuji Docker-client configuration contains unselected authority.")
        else:
            fd = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(expected)
        docker = client_cls(endpoint, client)
        docker.require_rootless_engine()
        campaign = FujiAcceptance(docker, pinned_images(), evidence, definition)
        primary = None
        failed_phase = None
        try:
            campaign.preflight()
            campaign.records["revision"] = campaign.child(["git", "rev-parse", "HEAD"], "source revision").decode().strip()
            campaign.records["source_dirty"] = bool(campaign.child(["git", "status", "--porcelain"], "source status").strip())
            campaign.start_fixture()
            campaign.historical()
            campaign.recent()
            campaign.records["checks"] = ["pinned-source", "gateway-write-denial", "direct-upstream-denial",
                "all-17-event-selection", "exact-creation-events", "historical-reader-state", "genuine-new-blocks",
                "clean-shutdown", "fresh-process-restart", "durable-prefix-continuity", "recent-reader-state"]
        except BaseException as error:
            primary = error
            failed_phase = campaign.records["phase"]
        finish_acceptance(campaign, primary, failed_phase)
