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
from fixture_protocol import FixtureError, _bad_constant, _pairs, decode, parse_phase_failure, receipt

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

def admit(rendered, *, runner_image, postgres_image, rpc_network):
    """Review the actual rendered Compose graph before creating SQL resources."""
    try:
        services = rendered["services"]
        networks = rendered["networks"]
        if (
            set(services) != {"postgres", "bootstrap", "initialize", "consumer"}
            or set(networks) != {"database", "rpc"}
            or networks["database"].get("internal") is not True
            or networks["rpc"].get("name") != rpc_network
            or networks["rpc"].get("external") is not True
        ):
            raise CampaignError("Fixture graph includes an unknown service or network.")

        for (name, service) in services.items():
            expected_image = postgres_image if name in ("postgres", "bootstrap") else runner_image
            required_user = "999:999" if name in ("postgres", "bootstrap") else "65532:65532"
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
                or any(item.get("type") == "bind" for item in service.get("volumes", []))
            ):
                raise CampaignError("Fixture service has an unapproved image, privilege or host attachment.")
            connected = set(service.get("networks", []))
            required = {"database", "rpc"} if name == "consumer" else {"database"}
            if connected != required:
                raise CampaignError("Fixture service has an unexpected database/RPC network grant.")

        owner = services["initialize"].get("environment", {})
        runtime = services["consumer"].get("environment", {})
        if (
            "CONIX_TEST_OWNER_DSN" not in owner or "CONIX_TEST_DSN" in owner
            or "CONIX_TEST_DSN" not in runtime or "CONIX_TEST_OWNER_DSN" in runtime
        ):
            raise CampaignError("One-shot owner credentials are not separated from the runtime.")

    except (TypeError, AttributeError, KeyError) as error:
        raise CampaignError("Cannot validate the rendered fixture service graph.") from None


def read_borrowed_descriptor(value):
    """Read a private, immutable handle to a producer-verified source descriptor.

    The descriptor authorizes an exclusive *test* against the supplied chain.
    It never grants Conix the right to stop or remove dapp32 resources.
    """
    if type(value) is not str or not value or "\x00" in value:
        raise CampaignError("FIXTURE_DESCRIPTOR must be an explicit absolute private JSON file.")

    path = Path(value)
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise CampaignError("FIXTURE_DESCRIPTOR must have an absolute path without symlink ancestors.")

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= 1048576):
                raise CampaignError("Fixture JSON must be an owner-owned, mode-0600-or-stricter regular file.")

            raw = os.read(fd, 1048577)
        finally:
            os.close(fd)
    except OSError:
        raise CampaignError("Cannot open the selected private fixture descriptor.") from None

    if len(raw) > 1048576:
        raise CampaignError("Private fixture descriptor exceeds its byte limit.")

    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=_bad_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise CampaignError("Fixture descriptor must be unique-key UTF-8 JSON.") from None

    if type(decoded) is not dict:
        raise CampaignError("Fixture descriptor must be a JSON object.")

    return decode(raw, fixture_id=decoded.get("fixtureId"))


def _compose(docker, project):
    """Select only Conix's disposable SQL/test Compose project."""
    return (
        "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
        "-p", project, "-f", str(ROOT / "compose/test-pay-to-play.yml"),
    )


def _json_request(descriptor, state):
    """Pass private fixture keys through stdin, never argv or environment."""
    return (json.dumps({"descriptor": descriptor, "state": state}, separators=(",", ":")) + "\n").encode()


class PayToPlayTest:
    """Only owned disposable SQL/test resources; never dapp32 lifecycle."""

    def __init__(self, docker, runner_image, scratch, descriptor):
        self.docker = docker
        self.runner_image = runner_image
        self.scratch = scratch
        self.descriptor = descriptor
        self.fixture_id = descriptor["fixtureId"]
        self.project = "conix-test-" + secrets.token_hex(6)
        self.sql_started = False
        self.sql_retired = True
        self.consumer_retired = True
        self.env = None

    def _file(self, name):
        return self.scratch / name

    def check_new_sql_project(self):
        """Refuse to adopt or destroy any pre-existing Compose project assets."""
        for kind in ("container", "network", "volume"):
            args = [kind, "ls", "--quiet", "--filter",
                    "label=com.docker.compose.project=" + self.project]
            if kind == "container":
                args.insert(2, "--all")

            if self.docker.run(*args, timeout=15, capture=True).strip():
                raise CampaignError(
                    "The generated Conix SQL project already has resources; "
                    "inspect ownership before starting or removing anything."
                )

    def sql(self):
        """Prepare isolated SQL, owner enrollment and the restricted test user."""
        images = pinned_images()
        self.docker.require_role(self.runner_image, "evm-test-runner")
        self.docker.require_image(images["postgres"])

        self.env = self.docker.docker_environment.copy()
        self.env.update(
            CONIX_EVM_TEST_RUNNER_IMAGE=self.runner_image,
            CONIX_POSTGRES_IMAGE=images["postgres"],
            CONIX_FIXTURE_ID=self.fixture_id,
            CONIX_RPC_NETWORK=self.descriptor["dockerNetwork"],
        )
        compose = _compose(self.docker, self.project)
        rendered_json = self.docker.run(*compose, "config", "--format", "json",
                                        capture=True, timeout=20, env=self.env)
        try:
            rendered = json.loads(rendered_json)
        except ValueError:
            raise CampaignError("Compose returned invalid rendered fixture JSON.") from None

        admit(
            rendered, runner_image=self.runner_image,
            postgres_image=images["postgres"], rpc_network=self.descriptor["dockerNetwork"],
        )
        self.check_new_sql_project()

        self.sql_retired = False
        self.sql_started = True
        _progress("Disposable PostgreSQL: starting")
        self.docker.run(*compose, "up", "--detach", "--no-build", "--pull", "never",
                        "--wait", "--wait-timeout", "60", "postgres",
                        env=self.env, timeout=90)
        _progress("Disposable PostgreSQL: ready")

        self._phase("bootstrap", "bootstrap", state=None, need_receipt=False)
        self._phase("initialize", "initialize", state=None, need_receipt=False)

    def _phase(self, service, phase, *, state, need_receipt):
        """Run the installed fixture phase and admit only its checked receipt."""
        argv = list(self.docker.docker_command_prefix) + list(_compose(self.docker, self.project)) + [
            "run", "--rm", "--no-deps", "--pull", "never", "-T",
            service, *([] if phase == "bootstrap" else [phase]),
        ]
        payload = _json_request(self.descriptor, state) if phase != "bootstrap" else None
        self.consumer_retired = False

        try:
            phase_output = owned_command(
                argv,
                output=self._file("conix-" + phase + ".out"),
                error_file=self._file("conix-" + phase + ".err"),
                env=self.env, timeout=180 if phase in ("collected", "reorg") else 75,
                payload=payload, label=f"Conix phase/{phase}",
            )
            self.consumer_retired = True
        except CampaignError as error:
            # Admit only a bounded phase failure from the installed script,
            # not arbitrary Docker stderr, Cast output or fixture secrets.
            try:
                stderr_tail = self._file("conix-" + phase + ".err").read_bytes()[-16384:]
            except OSError:
                stderr_tail = b""

            reason = parse_phase_failure(stderr_tail, phase=phase)
            if reason is not None:
                raise CampaignError(f"Conix phase/{phase} failed: {reason}.") from error

            # On timeout, a daemon-side one-shot process may still be running.
            raise CampaignError(f"Conix phase/{phase} failed: {error}") from error

        if not need_receipt:
            return None

        phase_receipt = receipt(phase_output)
        if phase_receipt["phase"] != phase:
            raise CampaignError("Contained fixture returned the wrong phase result.")

        return phase_receipt

    def execute(self):
        """Run all phases without acquiring or retiring the dapp32 fixture."""
        # The fixture is borrowed, but its chain state is changed deliberately
        # by the scenario's transactions and reorganization tests.
        verify_network(self.docker, self.descriptor)
        self.sql()

        previous_receipt = None
        for phase in ("baseline", "collected", "restart", "reorg"):
            previous_receipt = self._phase("consumer", phase, state=previous_receipt, need_receipt=True)
            if phase == "baseline" and previous_receipt["collection_count"] != 0:
                raise CampaignError("Baseline unexpectedly contains collection events.")

            if phase in ("collected", "restart") and previous_receipt["collection_count"] != 2:
                raise CampaignError("Expected two real indexed collection events.")

            if phase == "reorg" and previous_receipt["collection_count"] != 0:
                raise CampaignError("Orphaned collections remain after checked reorganization repair.")

    def cleanup(self):
        """Retire only this test's owned SQL project, regardless of outcome."""
        if not self.sql_started:
            return []

        try:
            self.docker.run(*_compose(self.docker, self.project),
                "down", "--volumes", "--remove-orphans", "--timeout", "30",
                env=self.env, timeout=100)
            self.sql_retired = True
            self.consumer_retired = True
            return []
        except Exception as error:
            detail = str(error) if isinstance(error, OperationError) else type(error).__name__
            return [f"Conix SQL/consumer teardown failed: {detail}; ownership unconfirmed."]

    def recovery(self):
        return (f"Borrowed fixture ID={self.fixture_id}; Conix project={self.project}; "
                "no rights to remove or replace the producer fixture; "
                f"selected Engine={self.docker.endpoint}.")


class Campaign:
    """Own producer fixture creation and retirement around the same test."""

    def __init__(self, docker, context, sibling, runner_image, scratch):
        self.docker = docker
        self.context = context
        self.sibling = sibling
        self.runner_image = runner_image
        self.scratch = scratch
        self.fixture_id = "conix-" + secrets.token_hex(6)
        self.descriptor = None
        self.producer_owned = False
        self.creation_attempted = False
        self.test = None

    def _file(self, name):
        return self.scratch / name

    def _dapp32(self, action):
        if action not in ("up", "info", "down"):
            raise CampaignError("Unknown producer fixture lifecycle action.")

        return owned_command(
            ["make", "--silent", "--no-print-directory", "-C", str(self.sibling),
             "pay-to-play/local/fixture/" + action,
             "DOCKER_CONTEXT=" + self.context,
             "FIXTURE_ID=" + self.fixture_id],
            output=self._file("producer-" + action + ".out"),
            error_file=self._file("producer-" + action + ".err"),
            env={**_clean_env(), "DOCKER_CONFIG": str(self.docker.config_dir)},
            timeout=480 if action == "up" else 150,
            label=("dapp32 fixture/up (includes build)" if action == "up"
                   else f"dapp32 fixture/{action}"),
        )

    def acquire(self):
        """Create and independently verify a new producer fixture."""
        # An attempted creation is not authority to remove it; ownership is
        # established only after the produced descriptor is checked.
        self.creation_attempted = True
        created_receipt = self._dapp32("up")
        descriptor = decode(created_receipt, fixture_id=self.fixture_id)

        _progress("dapp32 fixture: verifying Anvil network and deployment")
        verify_network(self.docker, descriptor)

        inspected = decode(self._dapp32("info"), fixture_id=self.fixture_id)
        if inspected != descriptor:
            raise CampaignError("Producer fixture changed between creation and independent inspection.")

        self.descriptor = descriptor
        self.producer_owned = True

    def execute(self):
        self.acquire()
        self.test = PayToPlayTest(self.docker, self.runner_image, self.scratch, self.descriptor)
        self.test.execute()

    def cleanup(self):
        """Retire consumers before the producer; report uncertain ownership."""
        errors = self.test.cleanup() if self.test is not None else []
        consumers_retired = self.test is None or (self.test.sql_retired and self.test.consumer_retired)

        if self.producer_owned and consumers_retired:
            try:
                self._dapp32("down")
                self.producer_owned = False
            except Exception as error:
                detail = str(error) if isinstance(error, CampaignError) else type(error).__name__
                errors.append(f"dapp32 producer fixture/down failed: {detail}; ownership unconfirmed.")

        if self.producer_owned or (self.creation_attempted and self.descriptor is None):
            errors.append(
                "Inspect the original dapp32 fixture ID on the selected Engine; "
                "creation/retirement ownership cannot be inferred from its name."
            )

        return errors

    def recovery(self):
        """Give exact operator coordinates without reproducing private JSON."""
        sql = self.test.project if self.test is not None else "none"
        return (
            f"Producer fixture ID={self.fixture_id}; Conix project={sql}; "
            f"Docker context={self.context}; selected Engine={self.docker.endpoint}. "
            f"Inspect: make -C {self.sibling} pay-to-play/local/fixture/info "
            f"DOCKER_CONTEXT={self.context} FIXTURE_ID={self.fixture_id}. "
            "Retire with the sibling's fixture/down only after verifying creation "
            "ownership and retirement of every consumer."
        )


def _signal_interruption(_signum, _frame):
    raise KeyboardInterrupt("Real-chain test interrupted; resources require reconciliation.")


def main():
    """Keep the primary failure last, after cleanup and recovery evidence."""
    signal.signal(signal.SIGTERM, _signal_interruption)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-make", action="store_true")
    parser.add_argument("--origin", required=True)
    parser.add_argument("action", choices=("test/pay-to-play/run", "campaign/pay-to-play/run"))
    parser.add_argument("--context")
    parser.add_argument("--dapp32-dir")
    parser.add_argument("--runner-image")
    parser.add_argument("--fixture-descriptor")
    args = parser.parse_args()

    try:
        if args.from_make:
            args.context = os.environ.get("CONIX_MAKE_DOCKER_CONTEXT", "")
            args.dapp32_dir = os.environ.get("CONIX_MAKE_DAPP32_DIR", "")
            args.runner_image = os.environ.get("CONIX_MAKE_RUNNER_IMAGE", "")
            args.fixture_descriptor = os.environ.get("CONIX_MAKE_FIXTURE_DESCRIPTOR", "")

        if not IMAGE_ID.fullmatch(args.runner_image or ""):
            raise CampaignError("Select a prebuilt immutable RUNNER_IMAGE=sha256:<id> with role evm-test-runner.")

        owned = args.action == "campaign/pay-to-play/run"
        if owned:
            sibling = sibling_checkout(args.dapp32_dir)
            if args.origin != "command line" or not args.context:
                raise CampaignError("The dapp32 campaign requires an explicit DOCKER_CONTEXT=<local-name>.")
        else:
            # Admission precedes ALL Docker access; no sibling checkout is used.
            borrowed = read_borrowed_descriptor(args.fixture_descriptor)

        endpoint = select_endpoint(args.context, args.origin)
        with tempfile.TemporaryDirectory(prefix="conix-test-client-") as directory:
            config = Path(directory)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}\n', encoding="utf-8")
            docker = Docker(endpoint, config)
            docker.require_rootless_engine()
            docker.require_role(args.runner_image, "evm-test-runner")

            if owned:
                pin_sibling_context(docker, args.context)

            scratch = Path(tempfile.mkdtemp(prefix="conix-pay-to-play-", dir="/tmp"))
            os.chmod(scratch, 0o700)
            operation = (
                Campaign(docker, args.context, sibling, args.runner_image, scratch) if owned
                else PayToPlayTest(docker, args.runner_image, scratch, borrowed)
            )
            failure = None

            try:
                operation.execute()
            except BaseException as error:
                failure = error

            try:
                cleanup = operation.cleanup()
            except BaseException as error:
                # Even a cleanup implementation error cannot hide the first
                # test failure or imply that upstream resources were retired.
                cleanup = [f"Cleanup operation raised {type(error).__name__}; owned resources require inspection."]
                if failure is None:
                    failure = error

            if failure is None and not cleanup:
                shutil.rmtree(scratch)
                print("PASS: PayToPlay collections, durable Conix indexing, "
                      "separate-session restart and checked manual reorganization repair.")
                return

            # Report recovery and cleanup first; end on the original failure.
            print(f"Private phase receipts: {scratch}", file=sys.stderr)
            print(f"Recovery: {operation.recovery()}", file=sys.stderr)

            if cleanup:
                for message in cleanup:
                    print(f"Cleanup unconfirmed: {message}", file=sys.stderr)
            else:
                print("Cleanup: confirmed for owned resources.", file=sys.stderr)

            if failure is not None:
                if isinstance(failure, KeyboardInterrupt):
                    detail = "Interrupted; inspect exact resource ownership."
                elif isinstance(failure, (CampaignError, FixtureError, OperationError)):
                    detail = f"{type(failure).__name__}: {failure}"
                else:
                    detail = f"{type(failure).__name__}: unexpected failure; inspect private receipts."
                if cleanup:
                    detail += " Cleanup is ALSO unconfirmed."

                raise CampaignError(f"PayToPlay campaign failed: {detail}") from failure
            raise CampaignError("PayToPlay campaign completed but cleanup could not be confirmed.")
    except (CampaignError, FixtureError, OperationError, OSError, ValueError) as error:
        raise SystemExit(f"Conix PayToPlay test refused or failed: {error}") from None


if __name__ == "__main__":
    main()
