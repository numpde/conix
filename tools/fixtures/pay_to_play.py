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
from pathlib import Path

from tools.containers.client import CONTEXT_NAME, IMAGE_ID, OperationError, select_endpoint
from tools.containers.operator import Docker, ROOT, pinned_images

from tools.fixtures.process import CampaignError, clean_environment, owned_command, progress

from tools.fixtures.protocol import (
    FixtureError,
    acceptance_preflight,
    acceptance_payments,
    acceptance_tokens,
    public_acceptance_source,
    public_test_source,
    decode,
    parse_phase_failure,
)

ID = re.compile(r"conix-[0-9a-f]{12}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")



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
                # Required finite-job resource ceilings belong to the
                # launched role, not merely to checked-in Compose prose.
                or service.get("cpus") is None
                or service.get("mem_limit") is None
                or service.get("memswap_limit") is None
                or type(service.get("pids_limit")) is not int
                or not 1 <= service["pids_limit"] <= 128
                or not service.get("tmpfs")
            ):
                raise CampaignError("Fixture service has an unapproved image, privilege or host attachment.")

            connected_networks = set(service.get("networks", []))
            required_networks = {"database", "rpc"} if service_name == "consumer" else {"database"}

            if connected_networks != required_networks:
                raise CampaignError("Fixture service has an unexpected database/RPC network grant.")

            if service_name == "consumer":
                runtime_tmpfs = service.get("tmpfs", [])
                if not any(
                    type(mount) is str
                    and mount.startswith("/run/conix:")
                    and all(part in mount.split(",") for part in (
                        "uid=65532", "gid=65532", "mode=0700",
                        "size=16m", "nosuid", "nodev", "noexec",
                    ))
                    for mount in runtime_tmpfs
                ):
                    raise CampaignError("Owned continuous acceptance needs a private runtime tmpfs.")

        owner_environment = rendered_services["initialize"].get("environment", {})
        consumer_environment = rendered_services["consumer"].get("environment", {})

        if (
            "CONIX_TEST_OWNER_DSN" not in owner_environment
            or "CONIX_TEST_DSN" in owner_environment
            or "CONIX_TEST_READ_DSN" in owner_environment
            or "CONIX_TEST_DSN" not in consumer_environment
            or "CONIX_TEST_READ_DSN" not in consumer_environment
            or "CONIX_TEST_OWNER_DSN" in consumer_environment
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

    # One strict parser owns JSON framing, duplicate keys, fixture identity
    # and complete deployment/account admission. Do not parse JSON twice.
    return decode(descriptor_bytes, fixture_id=None)


def _compose(docker, compose_project):
    """Select only Conix's disposable SQL/test Compose project."""
    return (
        "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
        "-p", compose_project, "-f", str(ROOT / "compose/test-pay-to-play.yml"),
    )


def _json_request(descriptor, prior_phase_receipt, *, tokens):
    """Send only the selected public phase source and bounded prior receipts."""
    return (json.dumps({"descriptor": descriptor, "state": prior_phase_receipt, "tokens": tokens}, separators=(",", ":")) + "\n").encode()


class PayToPlayTest:
    """Borrow the producer's Anvil; own only the disposable SQL/consumer project."""

    def __init__(self, docker, runner_image, scratch, descriptor, *, owned_fixture=False):
        self.preflight_record = None
        self.tokens = None
        self.owned_fixture = owned_fixture
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
        progress("Disposable PostgreSQL: starting")

        self.docker.run(
            *compose_argv, "up", "--detach", "--no-build", "--pull", "never",
            "--wait", "--wait-timeout", "60", "postgres",
            env=self.compose_environment, timeout=90,
        )
        progress("Disposable PostgreSQL: ready")

        self._phase("bootstrap", "bootstrap", prior_receipt=None, expect_receipt=False)

        # The consumer sees RPC but not owner credentials; the schema owner
        # receives only the checked public genesis and contract coordinates.
        self.preflight_record = self._phase(
            "consumer", "preflight", prior_receipt=None, expect_receipt=True,
        )
        if self.owned_fixture:
            # All selected source addresses must be known BEFORE owner
            # initializes the immutable event-history identity.
            self.tokens = self._phase(
                "consumer", "prepare",
                prior_receipt=self.preflight_record, expect_receipt=True,
            )
        self._phase(
            "initialize", "initialize",
            prior_receipt=self.preflight_record, expect_receipt=False,
        )

    def _phase(self, service_name, phase, *, prior_receipt, expect_receipt):
        """Run one installed fixture phase and admit its checked public receipt."""
        runner_arguments = (
            [service_name] if phase == "bootstrap"
            else ["--entrypoint", "python", service_name, "-I", "/tests/generic_acceptance.py", phase]
        )
        phase_argv = list(self.docker.docker_command_prefix) + list(_compose(self.docker, self.sql_project)) + [
            "run", "--rm", "--no-deps", "--pull", "never", "-T",
            *runner_arguments,
        ]
        # The owner gets source coordinates only; consumers get a public
        # ABI and role addresses. Neither phase receives fixture wallet keys.
        source = (
            public_acceptance_source(self.fixture_descriptor)
            if phase == "initialize"
            else public_test_source(self.fixture_descriptor)
        )
        phase_payload = _json_request(source, prior_receipt, tokens=self.tokens) if phase != "bootstrap" else None
        self.consumer_retired = False

        try:
            phase_stdout = owned_command(
                phase_argv,
                output=self._file("conix-" + phase + ".out"),
                error_file=self._file("conix-" + phase + ".err"),
                env=self.compose_environment,
                timeout=180 if phase == "exercise" else 120 if phase in ("prepare", "reorg", "continuous") else 75,
                payload=phase_payload,
                label=f"Conix phase/{phase}",
            )
            self.consumer_retired = True
        except CampaignError as error:
            # Child stderr is untrusted, even though its stdin excludes
            # producer wallet keys. Admit only its bounded phase-bound record.
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

        if phase == "preflight":
            return acceptance_preflight(phase_stdout, chain_id=self.fixture_descriptor["chainId"])
        if phase == "prepare" and self.owned_fixture:
            return acceptance_tokens(phase_stdout)
        if phase == "exercise" and self.owned_fixture:
            return acceptance_payments(phase_stdout)
        raise CampaignError("Unexpected receipt from a nonpublic acceptance phase.")

    def execute(self):
        """Run only the generic installed-package acceptance, no legacy path."""
        verify_network(self.docker, self.fixture_descriptor)
        self.prepare_sql()
        self._phase(
            "consumer", "baseline",
            prior_receipt=self.preflight_record, expect_receipt=False,
        )
        if self.owned_fixture:
            # Each phase starts a distinct short-lived container. A fresh
            # process verifies durable recovery; only our own Anvil can fork.
            observations = self._phase(
                "consumer", "exercise",
                prior_receipt=self.preflight_record, expect_receipt=True,
            )
            for phase in ("resume", "reorg", "continuous"):
                self._phase(
                    "consumer", phase,
                    prior_receipt=observations, expect_receipt=False,
                )

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

    def __init__(self, docker, context, sibling, runner_image, scratch):
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
            env={**clean_environment(), "DOCKER_CONFIG": str(self.docker.config_dir)},
            timeout=480 if action == "up" else 150,
            label="dapp32 fixture/up (includes build)" if action == "up" else f"dapp32 fixture/{action}",
        )

    def acquire(self):
        """Require independent evidence of creation before assuming cleanup rights."""
        # An attempted creation alone does not authorize deleting a producer.
        self.creation_attempted = True
        creation_receipt = self._dapp32("up")
        created_descriptor = decode(creation_receipt, fixture_id=self.fixture_id)

        progress("dapp32 fixture: verifying Anvil network and deployment")
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
            owned_fixture=True,
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
        "test/indexer/acceptance/borrowed/run", "test/indexer/acceptance/owned/run",
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

        owns_producer = options.action == "test/indexer/acceptance/owned/run"

        if owns_producer:
            sibling_checkout_path = sibling_checkout(options.dapp32_dir)

            if options.origin != "command line" or not options.context:
                raise CampaignError("The dapp32 campaign requires an explicit DOCKER_CONTEXT=<local-name>.")
        else:
            # Borrowed descriptors are admitted before any Docker authority.
            borrowed_descriptor = read_borrowed_descriptor(options.fixture_descriptor)

        docker_endpoint = select_endpoint(options.context, options.origin, working_dir=ROOT)

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
                    receipt_directory,
                )
                if owns_producer
                else PayToPlayTest(
                    docker, options.runner_image, receipt_directory, borrowed_descriptor,
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
                    "PASS: Generic Tier-0 acceptance campaign against installed code, "
                    "disposable PostgreSQL and real dapp32 Anvil."
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

                raise CampaignError(f"Generic acceptance campaign failed: {failure_detail}") from primary_failure

            raise CampaignError("Generic acceptance campaign completed but cleanup could not be confirmed.")
    except (CampaignError, FixtureError, OperationError, OSError, ValueError) as error:
        raise SystemExit(f"Conix generic acceptance refused or failed: {error}") from None


if __name__ == "__main__":
    main()
