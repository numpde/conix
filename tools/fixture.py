"""Own one disposable Conix integration campaign around dapp32's JSON fixture.

The producer repository is invoked, never modified. Its verified fixture JSON
is a private handoff, not a shell expression, log line or source of Docker
authority. This host tool owns only the new Conix SQL project and new fixture
it created; the producer owns Anvil, deployment and its cleanup operation.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile

from container import (
    Docker, IMAGE_ID, OperationError, ROOT, pinned_images, select_endpoint,
)

sys.path.insert(0, str(ROOT / "tests"))
from fixture_protocol import FixtureError, decode, receipt

ID = re.compile(r"conix-[0-9a-f]{12}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


class CampaignError(Exception):
    """The fixture operation or evidence was not reliably completed."""


def sibling_checkout(value):
    if type(value) is not str or not value or "\n" in value or "\r" in value:
        raise CampaignError("Choose a concrete DAPP32_DIR checkout, with no line breaks.")
    path = Path(value)
    if not path.is_absolute():
        raise CampaignError("DAPP32_DIR must be an explicit absolute checkout path.")
    if path.is_symlink() or not path.is_dir():
        raise CampaignError("The selected sibling checkout must be a real directory, not a symlink.")
    for name in ("Makefile", "tools/pay_to_play_fixture.sh", "tools/pay_to_play_fixture.py"):
        item = path / name
        if not item.is_file() or item.is_symlink():
            raise CampaignError("Sibling checkout is missing a regular dapp32 fixture contract file.")
    return path.resolve(strict=True)


def _clean_env():
    """Do not relay ambient Make, Compose, Docker or private-wallet authority."""
    excluded = (
        "DOCKER_", "COMPOSE_", "BUILDX_", "CONIX_MAKE_", "CAM_",
        "PAY_TO_PLAY_", "MAKE", "GNUMAKEFLAGS",
    )
    result = {
        k: v for k, v in os.environ.items()
        if not k.startswith(excluded) and k not in (
            "PRIVATE_KEY", "BASH_ENV", "ENV", "FIXTURE_ID", "SCENARIO",
        )
    }
    return result


def owned_command(argv, *, output, error_file, env, timeout, payload=None):
    """Bound a subprocess and its process group; never log private stdout."""
    with output.open("wb") as out, error_file.open("wb") as err:
        child = subprocess.Popen(
            argv, cwd=ROOT, env=env, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=out, stderr=err, start_new_session=True,
        )
        try:
            child.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            # The caller must still treat daemon effects as unknown. Child
            # grandchildren that detached into their own sessions may survive.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.communicate(timeout=5)
            raise CampaignError("A fixture operation exceeded its budget; resulting daemon effects are unconfirmed.")
    if child.returncode != 0:
        raise CampaignError("A fixture command failed; inspect its private diagnostic receipt and ownership before retrying.")
    if output.stat().st_size > 1048576:
        raise CampaignError("Fixture command produced an oversized receipt; no JSON was admitted.")
    return output.read_bytes()


def verify_network(docker, descriptor):
    project = descriptor["composeProject"]
    name = descriptor["dockerNetwork"]
    expected = f"true {project} {name}"
    observed = docker.run(
        "network", "inspect", "--format",
        '{{.Internal}} {{index .Labels "com.docker.compose.project"}} {{.Name}}',
        name, timeout=15, capture=True,
    ).strip()
    if observed != expected:
        raise CampaignError("dapp32 fixture network is not the internal, producer-owned network in the JSON.")
    running = docker.run(
        "ps", "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.service=pay-to-play-anvil",
        "--format", "{{.ID}}", timeout=15, capture=True,
    ).splitlines()
    if len(running) != 1 or re.fullmatch(r"[0-9a-f]{12,64}", running[0]) is None:
        raise CampaignError("Expected exactly one running producer-owned Anvil service.")


def admit(rendered, *, fixture_image, postgres_image, rpc_network):
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
        for name, service in services.items():
            expected_image = postgres_image if name in ("postgres", "bootstrap") else fixture_image
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


def _compose(docker, project):
    return (
        "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
        "-p", project, "-f", str(ROOT / "compose/fixture.yml"),
    )


def _json_request(descriptor, state):
    return (json.dumps({"descriptor": descriptor, "state": state}, separators=(",", ":")) + "\n").encode()


class Campaign:
    def __init__(self, docker, context, sibling, fixture_image, scratch):
        self.docker = docker
        self.context = context
        self.sibling = sibling
        self.fixture_image = fixture_image
        self.scratch = scratch
        self.fixture_id = "conix-" + secrets.token_hex(6)
        self.project = "conix-fixture-" + secrets.token_hex(6)
        self.descriptor = None
        self.producer_owned = False
        self.sql_started = False
        self.sql_retired = True
        self.consumer_retired = True
        self.env = None

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
            env=_clean_env(), timeout=480 if action == "up" else 150,
        )

    def acquire(self):
        # A deliberately new, unpredictable identity: never attach to or
        # automatically reset an existing user's fixture.
        raw = self._dapp32("up")
        descriptor = decode(raw, fixture_id=self.fixture_id)
        verify_network(self.docker, descriptor)
        same = decode(self._dapp32("info"), fixture_id=self.fixture_id)
        if same != descriptor:
            raise CampaignError("Producer fixture changed between creation and independent inspection.")
        self.descriptor = descriptor
        self.producer_owned = True

    def sql(self):
        images = pinned_images()
        self.docker.require_image(self.fixture_image)
        self.docker.require_image(images["postgres"])
        self.env = self.docker.env.copy()
        self.env.update(
            CONIX_FIXTURE_IMAGE=self.fixture_image,
            CONIX_POSTGRES_IMAGE=images["postgres"],
            CONIX_FIXTURE_ID=self.fixture_id,
            CONIX_RPC_NETWORK=self.descriptor["dockerNetwork"],
        )
        argv = _compose(self.docker, self.project)
        raw = self.docker.run(*argv, "config", "--format", "json",
                              capture=True, timeout=20, env=self.env)
        try:
            definition = json.loads(raw)
        except ValueError:
            raise CampaignError("Compose returned invalid rendered fixture JSON.") from None
        admit(
            definition, fixture_image=self.fixture_image,
            postgres_image=images["postgres"], rpc_network=self.descriptor["dockerNetwork"],
        )
        self.sql_retired = False
        self.sql_started = True
        self.docker.run(*argv, "up", "--detach", "--no-build", "--pull", "never",
                        "--wait", "--wait-timeout", "60", "postgres",
                        env=self.env, timeout=90)
        self._phase("bootstrap", "bootstrap", state=None, need_receipt=False)
        self._phase("initialize", "initialize", state=None, need_receipt=False)

    def _phase(self, service, phase, *, state, need_receipt):
        argv = list(self.docker.base) + list(_compose(self.docker, self.project)) + [
            "run", "--rm", "--no-deps", "--pull", "never", "-T",
            service, *([] if phase == "bootstrap" else [phase]),
        ]
        body = _json_request(self.descriptor, state) if phase != "bootstrap" else None
        self.consumer_retired = False
        try:
            result = owned_command(
                argv,
                output=self._file("conix-" + phase + ".out"),
                error_file=self._file("conix-" + phase + ".err"),
                env=self.env, timeout=180 if phase in ("collected", "reorg") else 75,
                payload=body,
            )
            self.consumer_retired = True
        except CampaignError:
            # A timed-out CLI may leave a daemon-side one-shot reader behind.
            # Do not automatically retire its upstream RPC fixture.
            raise
        if not need_receipt:
            return None
        current = receipt(result)
        if current["phase"] != phase:
            raise CampaignError("Contained fixture returned the wrong phase result.")
        return current

    def execute(self):
        self.acquire()
        self.sql()
        previous = None
        for phase in ("baseline", "collected", "restart", "reorg"):
            previous = self._phase("consumer", phase, state=previous, need_receipt=True)
            if phase == "baseline" and previous["collection_count"] != 0:
                raise CampaignError("Baseline unexpectedly contains collection events.")
            if phase in ("collected", "restart") and previous["collection_count"] != 2:
                raise CampaignError("Expected two real indexed collection events.")
            if phase == "reorg" and previous["collection_count"] != 0:
                raise CampaignError("Orphaned collections remain after checked reorganization repair.")

    def cleanup(self):
        errors = []
        if self.sql_started:
            try:
                self.docker.run(*_compose(self.docker, self.project),
                    "down", "--volumes", "--remove-orphans", "--timeout", "30",
                    env=self.env, timeout=100)
                self.sql_retired = True
                self.consumer_retired = True
            except Exception:
                errors.append("Conix SQL/consumer teardown is unconfirmed.")
        if self.producer_owned and self.sql_retired and self.consumer_retired:
            try:
                self._dapp32("down")
                self.producer_owned = False
            except Exception:
                errors.append("dapp32 fixture teardown is unconfirmed.")
        if self.producer_owned or self.descriptor is None:
            # Never delete a fixture named only by a partial, failed descriptor:
            # ownership cannot be inferred from the chosen name alone.
            errors.append(
                "Inspect the producer fixture with the exact dapp32 fixture/info command "
                "before operator-authorized retirement."
            )
        return errors

    def recovery(self):
        return (
            f"Fixture ID={self.fixture_id}; Conix project={self.project}; "
            f"selected Docker context={self.context}. "
            f"Inspect: make -C {self.sibling} pay-to-play/local/fixture/info "
            f"DOCKER_CONTEXT={self.context} FIXTURE_ID={self.fixture_id}. "
            f"If ownership is confirmed and all consumers are retired, remove with "
            f"pay-to-play/local/fixture/down using those same inputs."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-make", action="store_true")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--context")
    parser.add_argument("--dapp32-dir")
    parser.add_argument("--fixture-image")
    args = parser.parse_args()
    try:
        if args.from_make:
            args.context = os.environ.get("CONIX_MAKE_DOCKER_CONTEXT", "")
            args.dapp32_dir = os.environ.get("CONIX_MAKE_DAPP32_DIR", "")
            args.fixture_image = os.environ.get("CONIX_MAKE_CONIX_FIXTURE_IMAGE", "")
        if not IMAGE_ID.fullmatch(args.fixture_image or ""):
            raise CampaignError("Provide exact CONIX_FIXTURE_IMAGE=sha256:<id> built by tests/fixture/image/build.")
        sibling = sibling_checkout(args.dapp32_dir)
        endpoint = select_endpoint(args.context, args.origin)
        with tempfile.TemporaryDirectory(prefix="conix-fixture-client-") as directory:
            from container import environment
            config = Path(directory)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}\n')
            docker = Docker(endpoint, config)
            docker.require_rootless_engine()
            docker.require_image(args.fixture_image)
            scratch = Path(tempfile.mkdtemp(prefix="conix-fixture-", dir="/tmp"))
            os.chmod(scratch, 0o700)
            campaign = Campaign(docker, args.context, sibling, args.fixture_image, scratch)
            failure = None
            try:
                campaign.execute()
            except BaseException as error:
                failure = error
            cleanup = campaign.cleanup()
            if failure is None and not cleanup:
                shutil.rmtree(scratch)
                print("PASS: real dapp32 PayToPlay, installed Conix SQL, collections, restart and manual reorg repair.")
                return
            print(
                f"Conix fixture campaign is not qualified; private receipts retained at {scratch}. "
                + campaign.recovery(),
                file=sys.stderr,
            )
            if cleanup:
                for message in cleanup:
                    print(message, file=sys.stderr)
            if failure is not None:
                if isinstance(failure, KeyboardInterrupt):
                    raise CampaignError("Fixture campaign was interrupted; inspect exact ownership and receipts.")
                raise CampaignError(
                    f"Fixture campaign failed ({type(failure).__name__}); "
                    "inspect private receipts without publishing fixture keys."
                ) from None
            raise CampaignError("Fixture campaign cleanup could not be confirmed.")
    except (CampaignError, FixtureError, OperationError, OSError, ValueError) as error:
        raise SystemExit(f"Conix fixture refused or failed: {error}") from None


if __name__ == "__main__":
    main()
