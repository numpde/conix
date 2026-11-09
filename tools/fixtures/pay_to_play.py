"""Own one disposable Conix integration campaign around dapp32's JSON fixture.

The producer repository is invoked, never modified. Its verified fixture JSON
is a private handoff, not a shell expression, log line or source of Docker
authority. This host tool owns only the new Conix SQL project and new fixture
it created; the producer owns Anvil, deployment and its cleanup operation.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tools.containers.operator import (
    CONTEXT_NAME, Docker, IMAGE_ID, OperationError, ROOT, pinned_images, select_endpoint,
)

sys.path.insert(0, str(ROOT / "tests"))
from fixture_protocol import (FixtureError, _bad_constant, _pairs, acceptance_preflight,
                              public_acceptance_source, decode, parse_phase_failure, receipt)

ID = re.compile(r"conix-[0-9a-f]{12}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


class CampaignError(Exception):
    """The fixture operation or evidence was not reliably completed."""


def sibling_checkout(checkout_directory):
    """Admit a real sibling checkout without following the selected final link."""
    if type(checkout_directory) is not str or not checkout_directory or "\n" in checkout_directory or "\r" in checkout_directory:
        raise CampaignError("Choose a concrete DAPP32_DIR checkout, with no line breaks.")

    checkout_path = Path(checkout_directory)

    if not checkout_path.is_absolute():
        raise CampaignError("DAPP32_DIR must be an explicit absolute checkout path.")

    if checkout_path.is_symlink() or not checkout_path.is_dir():
        raise CampaignError("The selected sibling checkout must be a real directory, not a symlink.")

    for required_file in ("Makefile", "tools/pay_to_play_fixture.sh", "tools/pay_to_play_fixture.py"):
        fixture_file = checkout_path / required_file

        if not fixture_file.is_file() or fixture_file.is_symlink():
            raise CampaignError("Sibling checkout is missing a regular dapp32 fixture contract file.")

    return checkout_path.resolve(strict=True)


def pin_sibling_context(docker, context_name):
    """Bind the sibling's named context to this operation's admitted Unix socket.

    The producer resolves DOCKER_CONTEXT independently. A private Docker
    client configuration prevents external context changes mid-campaign.
    """
    if type(context_name) is not str or not CONTEXT_NAME.fullmatch(context_name):
        raise CampaignError("The sibling fixture requires one explicit valid Docker context name.")

    context_commands = (
        ("context", "create", context_name, "--docker", "host=" + docker.endpoint),
        ("context", "inspect", context_name, "--format", "{{.Endpoints.docker.Host}}"),
    )

    for context_args in context_commands:
        try:
            context_result = subprocess.run(
                ["docker", "--config", str(docker.config_dir), *context_args],
                env=docker.docker_environment,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CampaignError("Could not prepare the pinned sibling Docker client context.") from None

        if context_result.returncode:
            raise CampaignError(f"Pinned sibling Docker context {context_args[1]} failed (exit {context_result.returncode}).")

        if context_args[1] == "inspect" and context_result.stdout.strip() != docker.endpoint:
            raise CampaignError("Sibling Docker context resolves to a different Unix socket.")

    return None


def _clean_env():
    """Exclude ambient Docker, Make, Compose and private-wallet authority."""
    excluded_prefixes = (
        "DOCKER_", "COMPOSE_", "BUILDX_", "CONIX_MAKE_", "CAM_",
        "PAY_TO_PLAY_", "MAKE", "GNUMAKEFLAGS",
    )
    excluded_names = ("PRIVATE_KEY", "BASH_ENV", "ENV", "FIXTURE_ID", "SCENARIO")

    return {
        name: value for (name, value) in os.environ.items()
        if not name.startswith(excluded_prefixes) and name not in excluded_names
    }


def _progress(message):
    """Emit only source-controlled progress labels, never child output."""
    print(f"[conix] {message}", file=sys.stderr, flush=True)


def owned_command(argv, *, output, error_file, env, timeout, payload=None, label="fixture command"):
    """Bound and reap an owned child while keeping private receipts off terminal."""
    started_at = time.monotonic()
    _progress(f"{label}: started")

    with output.open("wb") as stdout_file, error_file.open("wb") as stderr_file:
        output.chmod(0o600)
        error_file.chmod(0o600)

        owned_process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )

        payload_pending = True

        try:
            while True:
                remaining_seconds = timeout - (time.monotonic() - started_at)

                if remaining_seconds <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)

                try:
                    owned_process.communicate(
                        input=payload if payload_pending else None,
                        timeout=min(15, remaining_seconds),
                    )
                    break
                except subprocess.TimeoutExpired:
                    # Resume the same child without resending its private stdin.
                    payload_pending = False

                    if time.monotonic() - started_at >= timeout:
                        raise

                    _progress(f"{label}: running ({int(time.monotonic() - started_at)}s)")
        except BaseException as error:
            # Reaping the CLI does not certify that Docker retired daemon work.
            try:
                os.killpg(owned_process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

            try:
                owned_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(owned_process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

                owned_process.wait(timeout=5)

            if isinstance(error, subprocess.TimeoutExpired):
                raise CampaignError(f"{label} timed out after {timeout}s; daemon effects are unconfirmed.") from error

            raise

    if owned_process.returncode != 0:
        raise CampaignError(f"{label} exited with status {owned_process.returncode}; see private {error_file.name}.")

    if output.stat().st_size > 1048576:
        raise CampaignError(f"{label} produced an oversized receipt; no JSON was admitted.")

    _progress(f"{label}: completed ({int(time.monotonic() - started_at)}s)")

    return output.read_bytes()


def verify_network(docker, descriptor):
    """Prove the descriptor points to a live producer-owned Anvil service."""
    producer_project = descriptor["composeProject"]
    network_name = descriptor["dockerNetwork"]
    expected_network_identity = f"true {producer_project} {network_name}"

    observed_network_identity = docker.run(
        "network", "inspect", "--format",
        '{{.Internal}} {{index .Labels "com.docker.compose.project"}} {{.Name}}',
        network_name, timeout=15, capture=True,
    ).strip()

    if observed_network_identity != expected_network_identity:
        raise CampaignError("dapp32 fixture network is not the internal, producer-owned network in the JSON.")

    running_anvil_ids = docker.run(
        "ps", "--filter", f"label=com.docker.compose.project={producer_project}",
        "--filter", "label=com.docker.compose.service=pay-to-play-anvil",
        "--format", "{{.ID}}", timeout=15, capture=True,
    ).splitlines()

    if len(running_anvil_ids) != 1 or re.fullmatch(r"[0-9a-f]{12,64}", running_anvil_ids[0]) is None:
        raise CampaignError("Expected exactly one running producer-owned Anvil service.")

def admit(rendered_graph, *, runner_image, postgres_image, rpc_network):
    """Review the rendered Compose graph before creating any SQL resource."""
    try:
        rendered_services = rendered_graph["services"]
        rendered_networks = rendered_graph["networks"]

        if (
            set(rendered_services) != {"postgres", "bootstrap", "initialize", "consumer"}
            or set(rendered_networks) != {"database", "rpc"}
            or rendered_networks["database"].get("internal") is not True
            or rendered_networks["rpc"].get("name") != rpc_network
            or rendered_networks["rpc"].get("external") is not True
        ):
            raise CampaignError("Fixture graph includes an unknown service or network.")

        for (service_name, service) in rendered_services.items():
            expected_image = postgres_image if service_name in ("postgres", "bootstrap") else runner_image
            required_user = "999:999" if service_name in ("postgres", "bootstrap") else "65532:65532"

            if (
                service.get("image") != expected_image
                or service.get("pull_policy") != "never"
                or service.get("user") != required_user
                or service.get("read_only") is not True
                or set(service.get("cap_drop", [])) != {"ALL"}
                or set(service.get("security_opt", [])) != {"no-new-privileges:true"}
                or service.get("privileged") or service.get("cap_add")
                or service.get("build") or service.get("ports")
                or service.get("network_mode") or service.get("devices")
                or service.get("pid") or service.get("ipc") or service.get("uts")
                or any(mount.get("type") == "bind" for mount in service.get("volumes", []))
            ):
                raise CampaignError("Fixture service has an unapproved image, privilege or host attachment.")

            connected_networks = set(service.get("networks", []))
            required_networks = {"database", "rpc"} if service_name == "consumer" else {"database"}

            if connected_networks != required_networks:
                raise CampaignError("Fixture service has an unexpected database/RPC network grant.")

        owner_environment = rendered_services["initialize"].get("environment", {})
        consumer_environment = rendered_services["consumer"].get("environment", {})

        if (
            "CONIX_TEST_OWNER_DSN" not in owner_environment or "CONIX_TEST_DSN" in owner_environment
            or "CONIX_TEST_DSN" not in consumer_environment or "CONIX_TEST_OWNER_DSN" in consumer_environment
        ):
            raise CampaignError("One-shot owner credentials are not separated from the runtime.")
    except (TypeError, AttributeError, KeyError) as error:
        raise CampaignError("Cannot validate the rendered fixture service graph.") from None


def read_borrowed_descriptor(descriptor_file):
    """Read a private test descriptor without acquiring producer cleanup rights.

    Its identity is checked again against the actual Docker network and RPC
    service; the descriptor alone never transfers ownership of dapp32 assets.
    """
    if type(descriptor_file) is not str or not descriptor_file or "\x00" in descriptor_file:
        raise CampaignError("FIXTURE_DESCRIPTOR must be an explicit absolute private JSON file.")

    descriptor_path = Path(descriptor_file)

    if not descriptor_path.is_absolute() or any(part.is_symlink() for part in (descriptor_path, *descriptor_path.parents)):
        raise CampaignError("FIXTURE_DESCRIPTOR must have an absolute path without symlink ancestors.")

    try:
        descriptor_fd = os.open(descriptor_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)

        try:
            file_stat = os.fstat(descriptor_fd)

            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or file_stat.st_mode & 0o077
                or not 0 < file_stat.st_size <= 1048576
            ):
                raise CampaignError("Fixture JSON must be an owner-owned, mode-0600-or-stricter regular file.")

            descriptor_bytes = os.read(descriptor_fd, 1048577)
        finally:
            os.close(descriptor_fd)
    except OSError:
        raise CampaignError("Cannot open the selected private fixture descriptor.") from None

    if len(descriptor_bytes) > 1048576:
        raise CampaignError("Private fixture descriptor exceeds its byte limit.")

    try:
        descriptor_document = json.loads(
            s=descriptor_bytes.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_bad_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise CampaignError("Fixture descriptor must be unique-key UTF-8 JSON.") from None

    if type(descriptor_document) is not dict:
        raise CampaignError("Fixture descriptor must be a JSON object.")

    return decode(descriptor_bytes, fixture_id=descriptor_document.get("fixtureId"))


def _compose(docker, compose_project):
    """Select only Conix's disposable SQL/test Compose project."""
    return (
        "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
        "-p", compose_project, "-f", str(ROOT / "compose/test-pay-to-play.yml"),
    )


def _json_request(descriptor, prior_phase_receipt):
    """Pass private fixture keys through stdin, never argv or environment."""
    return (json.dumps({"descriptor": descriptor, "state": prior_phase_receipt}, separators=(",", ":")) + "\n").encode()


class PayToPlayTest:
    """Borrow the producer's Anvil; own only the disposable SQL/consumer project."""

    def __init__(self, docker, runner_image, scratch, descriptor, *, acceptance=False):
        self.acceptance = acceptance
        self.preflight_record = None
        self.docker = docker
        self.runner_image = runner_image
        self.scratch_dir = scratch
        self.fixture_descriptor = descriptor
        self.fixture_id = descriptor["fixtureId"]
        self.sql_project = "conix-test-" + secrets.token_hex(6)
        self.sql_started = False
        self.sql_retired = True
        self.consumer_retired = True
        self.compose_environment = None

    def _file(self, receipt_filename):
        return self.scratch_dir / receipt_filename

    def check_new_sql_project(self):
        """Never adopt or delete a pre-existing Compose project."""
        for resource_kind in ("container", "network", "volume"):
            docker_argv = [
                resource_kind, "ls", "--quiet", "--filter",
                "label=com.docker.compose.project=" + self.sql_project,
            ]

            if resource_kind == "container":
                docker_argv.insert(2, "--all")

            if self.docker.run(*docker_argv, timeout=15, capture=True).strip():
                raise CampaignError(
                    "The generated Conix SQL project already has resources; "
                    "inspect ownership before starting or removing anything."
                )

    def prepare_sql(self):
        """Enroll the owner schema and restricted consumer in isolated PostgreSQL."""
        image_manifest = pinned_images()
        self.docker.require_role(self.runner_image, "evm-test-runner")
        self.docker.require_image(image_manifest["postgres"])

        self.compose_environment = self.docker.docker_environment.copy()
        self.compose_environment.update(
            CONIX_EVM_TEST_RUNNER_IMAGE=self.runner_image,
            CONIX_POSTGRES_IMAGE=image_manifest["postgres"],
            CONIX_FIXTURE_ID=self.fixture_id,
            CONIX_RPC_NETWORK=self.fixture_descriptor["dockerNetwork"],
        )

        compose_argv = _compose(self.docker, self.sql_project)
        rendered_compose_json = self.docker.run(
            *compose_argv, "config", "--format", "json",
            capture=True, timeout=20, env=self.compose_environment,
        )

        try:
            rendered_graph = json.loads(rendered_compose_json)
        except ValueError:
            raise CampaignError("Compose returned invalid rendered fixture JSON.") from None

        admit(
            rendered_graph,
            runner_image=self.runner_image,
            postgres_image=image_manifest["postgres"],
            rpc_network=self.fixture_descriptor["dockerNetwork"],
        )
        self.check_new_sql_project()

        # Once creation is attempted, teardown ownership persists even if
        # Docker returns an error before confirming startup.
        self.sql_retired = False
        self.sql_started = True
        _progress("Disposable PostgreSQL: starting")

        self.docker.run(
            *compose_argv, "up", "--detach", "--no-build", "--pull", "never",
            "--wait", "--wait-timeout", "60", "postgres",
            env=self.compose_environment, timeout=90,
        )
        _progress("Disposable PostgreSQL: ready")

        self._phase("bootstrap", "bootstrap", prior_receipt=None, expect_receipt=False)

        if self.acceptance:
            # The consumer can see RPC but not owner credentials. The owner
            # receives only the validated PUBLIC genesis preflight.
            self.preflight_record = self._phase(
                "consumer", "preflight", prior_receipt=None, expect_receipt=True,
            )
            self._phase(
                "initialize", "initialize",
                prior_receipt=self.preflight_record, expect_receipt=False,
            )
        else:
            self._phase("initialize", "initialize", prior_receipt=None, expect_receipt=False)

    def _phase(self, service_name, phase, *, prior_receipt, expect_receipt):
        """Run one installed fixture phase and admit its checked public receipt."""
        runner_arguments = (
            ["--entrypoint", "python", service_name, "-I", "/tests/generic_acceptance.py", phase]
            if self.acceptance and phase != "bootstrap"
            else [service_name, *([] if phase == "bootstrap" else [phase])]
        )
        phase_argv = list(self.docker.docker_command_prefix) + list(_compose(self.docker, self.sql_project)) + [
            "run", "--rm", "--no-deps", "--pull", "never", "-T",
            *runner_arguments,
        ]
        # The owner phase receives no wallet credentials or arbitrary
        # producer ABI contents. Its ABI is the reviewed test-image fixture.
        source = (
            public_acceptance_source(self.fixture_descriptor)
            if self.acceptance and phase == "initialize"
            else self.fixture_descriptor
        )
        phase_payload = _json_request(source, prior_receipt) if phase != "bootstrap" else None
        self.consumer_retired = False

        try:
            phase_stdout = owned_command(
                phase_argv,
                output=self._file("conix-" + phase + ".out"),
                error_file=self._file("conix-" + phase + ".err"),
                env=self.compose_environment,
                timeout=180 if phase in ("collected", "reorg") else 75,
                payload=phase_payload,
                label=f"Conix phase/{phase}",
            )
            self.consumer_retired = True
        except CampaignError as error:
            # A child may include wallet bytes in stderr; admit only its
            # source-controlled, phase-bound failure record.
            try:
                phase_stderr_tail = self._file("conix-" + phase + ".err").read_bytes()[-16384:]
            except OSError:
                phase_stderr_tail = b""

            verified_failure_reason = parse_phase_failure(phase_stderr_tail, phase=phase)

            if verified_failure_reason is not None:
                raise CampaignError(f"Conix phase/{phase} failed: {verified_failure_reason}.") from error

            # A timed-out CLI is not proof that daemon-side work has stopped.
            raise CampaignError(f"Conix phase/{phase} failed: {error}") from error

        if not expect_receipt:
            return None

        if self.acceptance and phase == "preflight":
            return acceptance_preflight(phase_stdout, chain_id=self.fixture_descriptor["chainId"])

        phase_receipt = receipt(phase_stdout)

        if phase_receipt["phase"] != phase:
            raise CampaignError("Contained fixture returned the wrong phase result.")

        return phase_receipt

    def execute(self):
        """Run the four phases without acquiring or retiring the producer fixture."""
        # This borrowed chain is intentionally modified with signed test
        # transactions, then with a controlled reorganization.
        verify_network(self.docker, self.fixture_descriptor)
        self.prepare_sql()

        if self.acceptance:
            self._phase(
                "consumer", "baseline",
                prior_receipt=self.preflight_record, expect_receipt=False,
            )
            return

        prior_receipt = None

        for phase in ("baseline", "collected", "restart", "reorg"):
            prior_receipt = self._phase("consumer", phase, prior_receipt=prior_receipt, expect_receipt=True)

            if phase == "baseline" and prior_receipt["collection_count"] != 0:
                raise CampaignError("Baseline unexpectedly contains collection events.")

            if phase in ("collected", "restart") and prior_receipt["collection_count"] != 2:
                raise CampaignError("Expected two real indexed collection events.")

            if phase == "reorg" and prior_receipt["collection_count"] != 0:
                raise CampaignError("Orphaned collections remain after checked reorganization repair.")

    def cleanup(self):
        """Retire only the owned SQL/consumer project, regardless of outcome."""
        if not self.sql_started:
            return []

        try:
            self.docker.run(
                *_compose(self.docker, self.sql_project),
                "down", "--volumes", "--remove-orphans", "--timeout", "30",
                env=self.compose_environment, timeout=100,
            )
            self.sql_retired = True
            self.consumer_retired = True

            return []
        except Exception as error:
            safe_failure_detail = str(error) if isinstance(error, OperationError) else type(error).__name__
            return [f"Conix SQL/consumer teardown failed: {safe_failure_detail}; ownership unconfirmed."]

    def recovery(self):
        return (
            f"Borrowed fixture ID={self.fixture_id}; Conix project={self.sql_project}; "
            "no rights to remove or replace the producer fixture; "
            f"selected Engine={self.docker.endpoint}."
        )


class Campaign:
    """Own a new dapp32 fixture and its Conix consumer until both are retired."""

    def __init__(self, docker, context, sibling, runner_image, scratch, *, acceptance=False):
        self.acceptance = acceptance
        self.docker = docker
        self.context_name = context
        self.sibling_checkout_path = sibling
        self.runner_image = runner_image
        self.scratch_dir = scratch
        self.fixture_id = "conix-" + secrets.token_hex(6)
        self.fixture_descriptor = None
        self.producer_owned = False
        self.creation_attempted = False
        self.consumer_test = None

    def _file(self, receipt_filename):
        return self.scratch_dir / receipt_filename

    def _dapp32(self, action):
        if action not in ("up", "info", "down"):
            raise CampaignError("Unknown producer fixture lifecycle action.")

        return owned_command(
            [
                "make", "--silent", "--no-print-directory", "-C", str(self.sibling_checkout_path),
                "pay-to-play/local/fixture/" + action,
                "DOCKER_CONTEXT=" + self.context_name,
                "FIXTURE_ID=" + self.fixture_id,
            ],
            output=self._file("producer-" + action + ".out"),
            error_file=self._file("producer-" + action + ".err"),
            env={**_clean_env(), "DOCKER_CONFIG": str(self.docker.config_dir)},
            timeout=480 if action == "up" else 150,
            label="dapp32 fixture/up (includes build)" if action == "up" else f"dapp32 fixture/{action}",
        )

    def acquire(self):
        """Require independent evidence of creation before assuming cleanup rights."""
        # An attempted creation alone does not authorize deleting a producer.
        self.creation_attempted = True
        creation_receipt = self._dapp32("up")
        created_descriptor = decode(creation_receipt, fixture_id=self.fixture_id)

        _progress("dapp32 fixture: verifying Anvil network and deployment")
        verify_network(self.docker, created_descriptor)

        inspected_descriptor = decode(self._dapp32("info"), fixture_id=self.fixture_id)

        if inspected_descriptor != created_descriptor:
            raise CampaignError("Producer fixture changed between creation and independent inspection.")

        self.fixture_descriptor = created_descriptor
        self.producer_owned = True

    def execute(self):
        self.acquire()
        self.consumer_test = PayToPlayTest(
            self.docker, self.runner_image, self.scratch_dir, self.fixture_descriptor,
            acceptance=self.acceptance,
        )
        self.consumer_test.execute()

    def cleanup(self):
        """Retire consumers first; preserve precise uncertainty about ownership."""
        cleanup_failures = self.consumer_test.cleanup() if self.consumer_test is not None else []
        consumers_retired = self.consumer_test is None or (
            self.consumer_test.sql_retired and self.consumer_test.consumer_retired
        )

        if self.producer_owned and consumers_retired:
            try:
                self._dapp32("down")
                self.producer_owned = False
            except Exception as error:
                safe_failure_detail = str(error) if isinstance(error, CampaignError) else type(error).__name__
                cleanup_failures.append(f"dapp32 producer fixture/down failed: {safe_failure_detail}; ownership unconfirmed.")

        if self.producer_owned or (self.creation_attempted and self.fixture_descriptor is None):
            cleanup_failures.append(
                "Inspect the original dapp32 fixture ID on the selected Engine; "
                "creation/retirement ownership cannot be inferred from its name."
            )

        return cleanup_failures

    def recovery(self):
        """Name exact resources without reproducing the private fixture descriptor."""
        consumer_project = self.consumer_test.sql_project if self.consumer_test is not None else "none"

        return (
            f"Producer fixture ID={self.fixture_id}; Conix project={consumer_project}; "
            f"Docker context={self.context_name}; selected Engine={self.docker.endpoint}. "
            f"Inspect: make -C {self.sibling_checkout_path} pay-to-play/local/fixture/info "
            f"DOCKER_CONTEXT={self.context_name} FIXTURE_ID={self.fixture_id}. "
            "Retire with the sibling's fixture/down only after verifying creation "
            "ownership and retirement of every consumer."
        )


def _signal_interruption(_signum, _frame):
    raise KeyboardInterrupt("Real-chain test interrupted; resources require reconciliation.")


def main():
    """Show cleanup status before the original failure, never raw private output."""
    signal.signal(signal.SIGTERM, _signal_interruption)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-make", action="store_true")
    parser.add_argument("--origin", required=True)
    parser.add_argument("action", choices=(
        "test/pay-to-play/run", "campaign/pay-to-play/run",
        "test/indexer/acceptance/run", "campaign/indexer/acceptance/run",
    ))
    parser.add_argument("--context")
    parser.add_argument("--dapp32-dir")
    parser.add_argument("--runner-image")
    parser.add_argument("--fixture-descriptor")
    options = parser.parse_args()

    try:
        if options.from_make:
            options.context = os.environ.get("CONIX_MAKE_DOCKER_CONTEXT", "")
            options.dapp32_dir = os.environ.get("CONIX_MAKE_DAPP32_DIR", "")
            options.runner_image = os.environ.get("CONIX_MAKE_RUNNER_IMAGE", "")
            options.fixture_descriptor = os.environ.get("CONIX_MAKE_FIXTURE_DESCRIPTOR", "")

        if not IMAGE_ID.fullmatch(options.runner_image or ""):
            raise CampaignError("Select a prebuilt immutable RUNNER_IMAGE=sha256:<id> with role evm-test-runner.")

        owns_producer = options.action.startswith("campaign/")
        acceptance = "/acceptance/" in options.action

        if owns_producer:
            sibling_checkout_path = sibling_checkout(options.dapp32_dir)

            if options.origin != "command line" or not options.context:
                raise CampaignError("The dapp32 campaign requires an explicit DOCKER_CONTEXT=<local-name>.")
        else:
            # Borrowed descriptors are admitted before any Docker authority.
            borrowed_descriptor = read_borrowed_descriptor(options.fixture_descriptor)

        docker_endpoint = select_endpoint(options.context, options.origin)

        with tempfile.TemporaryDirectory(prefix="conix-test-client-") as directory:
            client_config_directory = Path(directory)
            (client_config_directory / "config.json").write_text('{"auths":{},"proxies":{}}\n', encoding="utf-8")
            docker = Docker(docker_endpoint, client_config_directory)
            docker.require_rootless_engine()
            docker.require_role(options.runner_image, "evm-test-runner")

            if owns_producer:
                pin_sibling_context(docker, options.context)

            receipt_directory = Path(tempfile.mkdtemp(prefix="conix-pay-to-play-", dir="/tmp"))
            os.chmod(receipt_directory, 0o700)

            campaign_operation = (
                Campaign(
                    docker, options.context, sibling_checkout_path, options.runner_image,
                    receipt_directory, acceptance=acceptance,
                )
                if owns_producer
                else PayToPlayTest(
                    docker, options.runner_image, receipt_directory, borrowed_descriptor,
                    acceptance=acceptance,
                )
            )

            primary_failure = None

            try:
                campaign_operation.execute()
            except BaseException as error:
                primary_failure = error

            try:
                cleanup_failures = campaign_operation.cleanup()
            except BaseException as error:
                # An exceptional cleanup never erases the original failure or
                # authorizes retiring a resource whose ownership is uncertain.
                cleanup_failures = [
                    f"Cleanup operation raised {type(error).__name__}; owned resources require inspection."
                ]

                if primary_failure is None:
                    primary_failure = error

            if primary_failure is None and not cleanup_failures:
                shutil.rmtree(receipt_directory)
                print(
                    (
                        "PASS: Generic Tier-0 acceptance campaign against installed code, "
                        "disposable PostgreSQL and real dapp32 Anvil."
                        if acceptance else
                        "PASS: PayToPlay collections, durable Conix indexing, "
                        "separate-session restart and checked manual reorganization repair."
                    )
                )
                return

            # Retain private receipts for operator inspection, then report
            # cleanup and recovery before the primary failure LAST.
            print(f"Private phase receipts: {receipt_directory}", file=sys.stderr)
            print(f"Recovery: {campaign_operation.recovery()}", file=sys.stderr)

            if cleanup_failures:
                for cleanup_failure in cleanup_failures:
                    print(f"Cleanup unconfirmed: {cleanup_failure}", file=sys.stderr)
            else:
                print("Cleanup: confirmed for owned resources.", file=sys.stderr)

            if primary_failure is not None:
                if isinstance(primary_failure, KeyboardInterrupt):
                    failure_detail = "Interrupted; inspect exact resource ownership."
                elif isinstance(primary_failure, (CampaignError, FixtureError, OperationError)):
                    failure_detail = f"{type(primary_failure).__name__}: {primary_failure}"
                else:
                    failure_detail = (
                        f"{type(primary_failure).__name__}: unexpected failure; inspect private receipts."
                    )

                if cleanup_failures:
                    failure_detail += " Cleanup is ALSO unconfirmed."

                raise CampaignError(f"PayToPlay campaign failed: {failure_detail}") from primary_failure

            raise CampaignError("PayToPlay campaign completed but cleanup could not be confirmed.")
    except (CampaignError, FixtureError, OperationError, OSError, ValueError) as error:
        raise SystemExit(f"Conix PayToPlay test refused or failed: {error}") from None


if __name__ == "__main__":
    main()
