"""Own the deployment-acceptance fixture; boot Conix through ordinary Make.

The committed deployment is used in place. Only the generated deployment lock,
test database, RPC service and owned Docker resources are disposable. Captured
child output and recovery evidence stay under out/deployment-acceptance/.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import tomllib
import traceback

from tools.containers import named
from tools.containers.client import OperationError, select_endpoint
from tools.containers.deployment import _candidate_ids
from tools.fixtures.process import CampaignError, cancellation, clean_environment, owned_command, progress
from tools.wheelhouse.operations import SharedWheelhouse


NAME = "deployment-acceptance"
PROJECT = "conix-deployment-acceptance-fixture"
HELPER_LABEL = "io.conix.fixture=deployment-acceptance"
OWNER_LABEL = "io.conix.acceptance-owner"


def fixture_permissions(folder):
    """Install private modes Git cannot represent, without following links."""
    directories = (folder, folder / "abi", folder / "secrets")
    files = (folder / "deployment.toml", folder / "abi/erc20.json",
             folder / "secrets/runtime.dsn", folder / "secrets/owner.dsn")
    for path in directories + files:
        info = path.lstat()
        expected = stat.S_ISDIR if path in directories else stat.S_ISREG
        if not expected(info.st_mode) or info.st_uid != os.geteuid():
            raise OperationError("Acceptance inputs must be owned files/directories without symlinks.")
        path.chmod(0o700 if path in directories else 0o600)


@contextmanager
def campaign_lock(folder):
    """Serialize the full test separately from the normal deployment locks."""
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OperationError("Acceptance evidence directory must be owner-only (0700).")
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OperationError("A deployment acceptance run already owns this checkout.") from error
        yield
    finally:
        os.close(descriptor)


def admit_fixture(graph, images, owner):
    """Refuse unreviewed images, host access or external fixture networks."""
    try:
        services = graph["services"]
        networks = graph["networks"]
        if set(services) != {"postgres", "anvil", "bootstrap"} or set(networks) != {"tests", "rpc"}:
            raise OperationError("Deployment acceptance requires exactly its reviewed fixture graph.")
        for name, service in services.items():
            expected = images["foundry"] if name == "anvil" else images["postgres"]
            if (service.get("image") != expected or service.get("pull_policy") != "never"
                    or service.get("read_only") is not True
                    or service.get("user") != ("65532:65532" if name == "anvil" else "999:999")
                    or service.get("privileged") or service.get("ports") or service.get("build")
                    or service.get("network_mode") or service.get("cap_add")
                    or service.get("devices") or service.get("pid")
                    or set(service.get("cap_drop", [])) != {"ALL"}
                    or service.get("security_opt") != ["no-new-privileges:true"]
                    or service.get("labels", {}).get(OWNER_LABEL) != owner
                    or any(mount.get("type") != "volume" for mount in service.get("volumes", []))):
                raise OperationError("Deployment acceptance fixture violates its image or host-access grants.")
        if any(network.get("internal") is not True or network.get("external")
               or network.get("labels", {}).get(OWNER_LABEL) != owner for network in networks.values()):
            raise OperationError("Deployment acceptance fixture networks must be owned internal bridges.")
        volumes = graph["volumes"]
        if (set(volumes) != {"pgdata"} or volumes["pgdata"].get("external")
                or volumes["pgdata"].get("driver_opts")
                or volumes["pgdata"].get("labels", {}).get(OWNER_LABEL) != owner):
            raise OperationError("Deployment acceptance requires its own ordinary database volume.")
    except (KeyError, TypeError, AttributeError) as error:
        raise OperationError("Cannot admit the deployment acceptance fixture graph.") from error


class DeploymentAcceptance:
    """Exercise one stable named deployment and retire only this test's resources."""

    def __init__(self, decl, docker, images, evidence):
        self.decl = decl
        self.docker = docker
        self.images = images
        self.evidence = evidence
        self.runner = None
        self.fixture_attempted = False
        self.prepare_attempted = False
        self.phase = "preflight"
        self.step = 0
        self.records = {"deployment": NAME, "endpoint": docker.endpoint, "checks": []}
        self.owner = secrets.token_hex(12)
        self.compose = docker.docker_command_prefix + [
            "compose", "--env-file", "/dev/null", "--project-directory", str(named.ROOT),
            "-p", PROJECT, "-f", str(named.ROOT / "compose/deployment-test.yml"),
        ]
        self.environment = docker.docker_environment.copy()
        self.environment["CONIX_ACCEPTANCE_OWNER"] = self.owner

    def require(self, condition, reason):
        """An acceptance failure names a source-owned contract, never borrowed prose."""
        if not condition:
            raise OperationError(reason)

    def child(self, argv, label, *, payload=None, environment=None, timeout=90):
        """Keep process progress, deadlines and private output under the shared owner."""
        self.phase = label
        self.step += 1
        prefix = self.evidence / f"{self.step:03d}"
        return owned_command(
            argv, output=prefix.with_suffix(".out"), error_file=prefix.with_suffix(".err"),
            env=self.environment if environment is None else environment,
            timeout=timeout, payload=payload, label=label,
        )

    def make(self, action):
        """Use the public deployment command, with its saved resource selection."""
        endpoint = select_endpoint(self.decl.docker_context, "command line", working_dir=named.ROOT)
        self.require(endpoint == self.docker.endpoint, "The saved context changed during acceptance.")
        return self.child(
            ["make", "--no-print-directory", action, "DEPLOYMENT=" + NAME], action,
            environment=clean_environment(), timeout=660,
        )

    def preflight(self):
        """Never adopt a previous deployment, generated lock or leftover fixture."""
        self.require(named.inspect_prepared(NAME) is None,
                     "Acceptance found an existing prepared deployment; inspect it before another run.")
        self.require(not _candidate_ids(self.docker, "conix-" + NAME),
                     "Acceptance found an existing indexer; it will not be adopted or stopped.")
        self.require(not self.docker.run("ps", "--all", "--filter",
                     "label=com.docker.compose.project=" + PROJECT, "--format", "{{.ID}}", capture=True).strip(),
                     "Acceptance fixture containers already exist; inspect their owner.")
        for resource, names in (("network", (PROJECT + "_tests", PROJECT + "_rpc")),
                                ("volume", (PROJECT + "_pgdata",))):
            for name in names:
                observed = self.docker.run(resource, "ls", "--filter", "name=" + name,
                                           "--format", "{{.Name}}", capture=True).splitlines()
                self.require(name not in observed, "Acceptance found a retained fixture resource; inspect it first.")
        helpers = self.docker.run("ps", "--all", "--filter", "label=" + HELPER_LABEL,
                                  "--format", "{{.ID}}", capture=True)
        self.require(not helpers.strip(), "An acceptance helper remains; inspect its owner before another run.")
        self.require(self.decl.database_network == PROJECT + "_tests"
                     and self.decl.rpc_network == PROJECT + "_rpc",
                     "Acceptance profile must select its dedicated fixture networks.")

    def probe(self, operation, *, empty_blocks=0):
        """Observe the fixture from a confined installed runner with stdin-only inputs."""
        request = {"operation": operation}
        network = self.decl.rpc_network
        if operation == "snapshot":
            network = self.decl.database_network
            request["dsn"] = self.docker.private_bytes(self.decl.runtime_dsn_file, 8192, secret=True).decode()
        elif operation == "produce":
            request["empty_blocks"] = empty_blocks
        argv = self.docker.docker_command_prefix + [
            "run", "--rm", "--interactive", "--pull", "never", "--label", HELPER_LABEL,
            "--label", OWNER_LABEL + "=" + self.owner,
            "--network", network, "--read-only", "--user", "65532:65532",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "32", "--memory", "128m", "--memory-swap", "128m",
            # The read-only image owns the script directory and its test helpers.
            # Ignore Python environment/user-site settings while allowing sibling imports.
            "--entrypoint", "python", self.runner, "-E", "-s", "/tests/deployment_probe.py"]
        try:
            output = self.child(argv, "fixture " + operation, payload=json.dumps(request).encode(), timeout=30)
        except CampaignError as error:
            # Admit only this helper's bounded type/location record, never
            # arbitrary subprocess stderr or database/RPC exception values.
            diagnostic = (self.evidence / f"{self.step:03d}.err").read_bytes()
            if re.fullmatch(rb"Deployment probe failed \([A-Za-z][A-Za-z0-9_]{0,63}\) "
                            rb"at [A-Za-z0-9_]{1,64}\.py:[1-9][0-9]{0,5}\.\n?", diagnostic):
                raise OperationError(diagnostic.decode("ascii").strip()) from error
            raise
        try:
            result = json.loads(output)
            if type(result) is not dict:
                raise ValueError()
            return result
        except (ValueError, UnicodeError) as error:
            raise OperationError("Acceptance helper returned no valid observation.") from error

    def running(self):
        """Read one immutable, image-pinned retained process identity."""
        identities = _candidate_ids(self.docker, "conix-" + NAME)
        self.require(len(identities) == 1, "Acceptance requires exactly one retained indexer.")
        (identity,) = identities
        record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
        prepared = named.inspect_prepared(NAME)
        self.require(prepared is not None and record["Id"] == identity
                     and record["State"]["Running"]
                     and record["Image"] == prepared.image
                     and record["Config"]["Cmd"] == ["-I", "-m", "conix", "run"],
                     "The installed retained process exited or changed identity.")
        return identity

    def await_height(self, height):
        """Bound observation without retrying or replacing failed application work."""
        self.require(type(height) is int and 1 <= height <= 32, "Fixture height exceeds acceptance bounds.")
        deadline = time.monotonic() + 35
        while True:
            identity = self.running()
            state = self.probe("snapshot")
            self.require(state["blocked"] is None, "The retained journal became blocked.")
            if state["height"] == height:
                self.require([row[0] for row in state["blocks"]] == list(range(1, height + 1)),
                             "Retained history is not a complete block prefix.")
                return identity, state
            self.require(time.monotonic() < deadline, "The retained indexer did not reach the fixture head.")
            time.sleep(0.25)

    def stop(self, identity):
        """Use normal retirement and prove SIGTERM completed with exit status zero."""
        since = str(int(time.time()) - 1)
        self.make("deployment/logs")
        self.make("deployment/down")
        events = self.docker.run("events", "--since", since, "--until", str(int(time.time()) + 1),
            "--filter", "container=" + identity, "--filter", "event=die", "--format", "{{json .}}",
            capture=True, timeout=10)
        exits = [json.loads(line)["Actor"]["Attributes"].get("exitCode") for line in events.splitlines()]
        self.require(exits == ["0"], "Retained shutdown did not confirm a clean process exit.")
        self.require(not _candidate_ids(self.docker, "conix-" + NAME), "Retained shutdown left a container.")
        self.make("deployment/status")

    def execute(self):
        """Provision dependencies, then exercise the same public lifecycle as an operator."""
        self.preflight()
        self.records["revision"] = self.child(["git", "rev-parse", "HEAD"], "source revision").decode().strip()
        for role in ("python_base", "postgres", "foundry"):
            self.docker.require_image(self.images[role])
        self.phase = "offline acceptance image build"
        self.runner = SharedWheelhouse(self.docker, self.images, self.decl.wheelhouse).build("test-runner")
        self.environment.update(CONIX_TEST_RUNNER_IMAGE=self.runner,
            CONIX_POSTGRES_IMAGE=self.images["postgres"], CONIX_FOUNDRY_IMAGE=self.images["foundry"])
        graph = json.loads(self.child(self.compose + ["config", "--format", "json"], "fixture admission"))
        admit_fixture(graph, self.images, self.owner)
        # Mark the creation boundary before the call: a client error does not
        # establish that the daemon created nothing.
        self.fixture_attempted = True
        self.child(self.compose + ["up", "--detach", "--no-build", "--pull", "never",
                   "--wait", "--wait-timeout", "60", "postgres", "anvil"], "fixture startup")
        self.child(self.compose + ["run", "--rm", "--no-deps", "--pull", "never",
                   "--interactive=false", "-T", "bootstrap"], "fixture roles and schema")
        genesis = self.probe("genesis")
        self.records["genesis"] = genesis["hash"]
        self.require(genesis["hash"] == self.decl.genesis_hash,
                     "Fixture genesis differs from the committed deployment identity.")
        first_block = self.probe("produce", empty_blocks=3)
        self.prepare_attempted = True
        self.make("deployment/prepare")
        prepared = named.inspect_prepared(NAME)
        self.require(prepared is not None, "Preparation returned without an admitted selection.")
        self.make("deployment/database/schema/init")
        self.make("deployment/config/check")
        self.make("deployment/up")
        first_id, first = self.await_height(first_block["height"])
        self.require(any(row[1] == first_block["transaction"][2:] for row in first["events"]),
                     "Initial catch-up omitted the fixture Transfer.")
        self.make("deployment/status")
        time.sleep(2)
        self.require(self.probe("snapshot") == first, "Idle tip changed durable progress.")
        arrived = self.probe("produce", empty_blocks=1)
        same_id, before = self.await_height(arrived["height"])
        self.require(same_id == first_id, "Tip following replaced the retained process.")
        self.stop(first_id)
        self.require(self.probe("snapshot") == before, "Shutdown changed durable history.")
        backlog = self.probe("produce", empty_blocks=2)
        self.require(self.probe("snapshot") == before, "A stopped deployment indexed new blocks.")
        self.make("deployment/up")
        second_id, resumed = self.await_height(backlog["height"])
        self.require(second_id != first_id, "Restart did not create a fresh process.")
        self.require(resumed["source_id"] == before["source_id"] and resumed["generation"] == before["generation"]
                     and resumed["blocks"][:len(before["blocks"])] == before["blocks"]
                     and resumed["events"][:len(before["events"])] == before["events"],
                     "Restart changed the previously retained history or source identity.")
        latest = self.probe("produce", empty_blocks=1)
        same_id, final = self.await_height(latest["height"])
        self.require(same_id == second_id and len(final["events"]) == 4,
                     "The restarted service did not follow all fixture events.")
        self.stop(second_id)
        self.require(self.probe("snapshot") == final and named.inspect_prepared(NAME) == prepared,
                     "Final retirement changed durable history or the prepared selection.")
        self.records.update(first_container=first_id, second_container=second_id,
                            before_restart=before, after_restart=resumed, final=final)
        self.records["checks"] = ["catch-up", "tip-following", "clean-shutdown", "fresh-process-restart",
                                   "durable-prefix-continuity", "tip-after-restart"]

    def verify_fixture_ownership(self):
        """Discovery by stable project name never authorizes deleting another run's state."""
        identities = self.docker.run("ps", "--all", "--no-trunc", "--filter",
            "label=com.docker.compose.project=" + PROJECT, "--format", "{{.ID}}", capture=True).splitlines()
        for identity in identities:
            record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
            labels = record["Config"]["Labels"]
            role = labels.get("com.docker.compose.service")
            expected = self.images["foundry"] if role == "anvil" else self.images["postgres"]
            self.require(record["Id"] == identity and labels.get(OWNER_LABEL) == self.owner
                         and role in ("postgres", "anvil", "bootstrap")
                         and record["Config"]["Image"] == expected,
                         "Fixture container ownership changed; external resources were not retired.")
        for resource, names in (("network", (PROJECT + "_tests", PROJECT + "_rpc")),
                                ("volume", (PROJECT + "_pgdata",))):
            for name in names:
                found = self.docker.run(resource, "ls", "--filter", "name=" + name,
                                        "--format", "{{.Name}}", capture=True).splitlines()
                if name in found:
                    record = json.loads(self.docker.run(resource, "inspect", name, capture=True))[0]
                    self.require(record["Name"] == name and record["Labels"].get(OWNER_LABEL) == self.owner,
                                 "Fixture network/volume ownership changed; external resources were not retired.")

    def cleanup(self):
        """Do not retire borrowed services while the named consumer may still be alive."""
        failures = []
        if not self.fixture_attempted:
            return failures
        try:
            self.make("deployment/down")
            self.require(not _candidate_ids(self.docker, "conix-" + NAME), "Named consumer retirement is unconfirmed.")
        except BaseException as error:
            return ["named consumer retirement (" + type(error).__name__ + ")"]
        if self.prepare_attempted:
            try:
                prepared = named.inspect_prepared(NAME)
                if prepared is not None:
                    self.docker.remove_inputs(prepared.volume)
                    named.clear_prepared(NAME, expected_volume=prepared.volume)
            except BaseException as error:
                failures.append("staged inputs (" + type(error).__name__ + ")")
        try:
            helpers = self.docker.run("ps", "--all", "--no-trunc", "--filter", "label=" + HELPER_LABEL,
                                      "--format", "{{.ID}}", capture=True).splitlines()
            for identity in helpers:
                record = json.loads(self.docker.run("container", "inspect", "--format", "{{json .}}", identity, capture=True))
                self.require(record["Id"] == identity and record["Config"]["Labels"].get("io.conix.fixture") == NAME,
                             "Acceptance helper ownership changed before cleanup.")
                self.require(record["Config"]["Labels"].get(OWNER_LABEL) == self.owner,
                             "Acceptance helper belongs to another run.")
                self.docker.run("container", "rm", "--force", identity, capture=True)
            self.verify_fixture_ownership()
            self.child(self.compose + ["down", "--volumes", "--remove-orphans", "--timeout", "30"], "fixture retirement")
            for resource, flag in (("ps", "--all"), ("network", "ls"), ("volume", "ls")):
                observed = self.docker.run(resource, flag, "--filter", "label=com.docker.compose.project=" + PROJECT,
                                           "--format", "{{.ID}}" if resource != "volume" else "{{.Name}}", capture=True)
                self.require(not observed.strip(), "Acceptance fixture retirement left owned resources.")
        except BaseException as error:
            failures.append("fixture retirement (" + type(error).__name__ + ")")
        return failures


def run_acceptance(options, *, client_cls, pinned_images):
    """Select the committed profile, preserve primary failure and report cleanup separately."""
    if options.deployment and options.deployment != NAME:
        raise OperationError("The deployment acceptance lane owns DEPLOYMENT=deployment-acceptance only.")
    folder = named._directory(NAME)
    fixture_permissions(folder)
    decl = named.inspect_named(NAME)
    if options.context and options.context != decl.docker_context:
        raise OperationError("Use the acceptance profile's saved docker_context.")
    if options.wheelhouse and options.wheelhouse != decl.wheelhouse:
        raise OperationError("Use the acceptance profile's saved wheelhouse.")
    evidence = named.ROOT / "out" / NAME
    with campaign_lock(evidence), cancellation():
        endpoint = select_endpoint(decl.docker_context, "command line", working_dir=named.ROOT)
        client = evidence / "docker-client"
        client.mkdir(mode=0o700, exist_ok=True)
        (client / "config.json").write_text('{"auths":{},"proxies":{}}\n')
        docker = client_cls(endpoint, client)
        docker.require_rootless_engine()
        campaign = DeploymentAcceptance(decl, docker, pinned_images(), evidence)
        primary = None
        try:
            campaign.execute()
        except BaseException as error:
            primary = error
            failed_phase = campaign.phase
        try:
            failures = campaign.cleanup()
        except BaseException as error:
            failures = ["acceptance cleanup (" + type(error).__name__ + ")"]
        campaign.records.update(passed=primary is None and not failures, cleanup_errors=failures)
        report = evidence / "result.json"
        report.write_text(json.dumps(campaign.records, indent=2) + "\n")
        report.chmod(0o600)
        for failure in failures:
            print("Cleanup unconfirmed: " + failure, file=sys.stderr)
        if primary is not None:
            # Do not stringify arbitrary exceptions or captured child output.
            if isinstance(primary, (OperationError, CampaignError)):
                detail = str(primary)
            else:
                frame = traceback.extract_tb(primary.__traceback__)[-1]
                detail = f"{type(primary).__name__} at {Path(frame.filename).name}:{frame.lineno}"
            raise OperationError(f"Deployment acceptance failed during {failed_phase}: {detail}. Evidence: {evidence}") from primary
        if failures:
            raise OperationError(f"Deployment acceptance cleanup is unconfirmed. Evidence: {evidence}")
        progress(f"PASS: retained deployment acceptance; evidence: {report}")
