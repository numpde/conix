"""The sibling fixture runner's ownership and admission contract, no Docker.

Exercises failures before acquisition, descriptor/endpoint provenance, rendered
privileges and teardown order without inventing a fake indexer or signed logs.
"""
import io
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
from tools.fixtures import pay_to_play as runner
from tools.fixtures.protocol import FixtureError, ScenarioFailure, phase_failure_record
from test_fixture_protocol import valid


def descriptor_for(fixture_id):
    """Give tests a producer-shaped descriptor without a running chain."""
    document = valid()
    project = "dapps-pay-to-play-fixture-" + fixture_id
    document["fixtureId"] = fixture_id
    document["composeProject"] = project
    document["dockerNetwork"] = project + "_pay-to-play_local"
    return document


def graph(fixture_image, postgres_image, rpc):
    """Build an admitted Compose graph for adversarial privilege changes."""
    def item(image, user, networks, environment=None):
        return {
            "image": image, "pull_policy": "never", "user": user,
            "read_only": True, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "networks": networks, "environment": environment or {},
        }
    return {
        "services": {
            "postgres": item(postgres_image, "999:999", {"database": None}),
            "bootstrap": item(postgres_image, "999:999", {"database": None}),
            "initialize": item(fixture_image, "65532:65532", {"database": None}, {
                "CONIX_TEST_OWNER_DSN": "test-only:owner",
            }),
            "consumer": item(fixture_image, "65532:65532", {
                "database": None, "rpc": None,
            }, {"CONIX_TEST_DSN": "test-only:runtime",
                "CONIX_TEST_READ_DSN": "test-only:reader"}),
        },
        "networks": {"database": {"internal": True},
                     "rpc": {"external": True, "name": rpc}},
    }


class FakeDocker:
    """Record only the calls needed to check ownership and admission order."""

    def __init__(self):
        self.calls = []
        self.docker_environment = {}

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("network", "inspect"):
            return os.environ.get("CONIX_TEST_NETWORK_INSPECT", "")

        if args and args[0] == "ps":
            return "0123456789ab\n"

        return None


class FixtureRunnerTests(unittest.TestCase):
    """Keep dapp32 producer authority separate from Conix's borrowed test."""

    def test_sibling_context_is_frozen_in_private_docker_client(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = SimpleNamespace(
                endpoint="unix:///tmp/selected-engine.sock",
                config_dir=Path(folder),
                docker_environment={"DOCKER_CONFIG": folder, "COMPOSE_DISABLE_ENV_FILE": "1"},
            )
            commands = []
            def controlled(argv, **kwargs):
                commands.append((argv, kwargs))
                if "inspect" in argv:
                    return SimpleNamespace(returncode=0, stdout=selected.endpoint + "\n")

                return SimpleNamespace(returncode=0, stdout="created\n")
            with patch.object(runner.subprocess, "run", side_effect=controlled):
                runner.pin_sibling_context(selected, "science")
            self.assertEqual(len(commands), 2)
            for (argv, kwargs) in commands:
                self.assertEqual(argv[:3], ["docker", "--config", folder])
                self.assertEqual(kwargs["env"]["DOCKER_CONFIG"], folder)
            self.assertIn("host=" + selected.endpoint, commands[0][0])
            with patch.object(runner.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout="created\n"),
                SimpleNamespace(returncode=0, stdout="unix:///tmp/foreign.sock\n"),
            ]):
                with self.assertRaises(runner.CampaignError):
                    runner.pin_sibling_context(selected, "science")

    def test_sibling_make_receives_only_private_bound_context(self):
        with tempfile.TemporaryDirectory() as folder:
            docker = FakeDocker()
            docker.config_dir = Path(folder)
            campaign = runner.Campaign(
                docker, "science", Path(folder), "sha256:" + "a" * 64,
                Path(folder),
            )
            with patch.object(runner, "owned_command", return_value=b"{}") as invoke:
                campaign._dapp32("info")
            argv = invoke.call_args.args[0]
            self.assertIn("DOCKER_CONTEXT=science", argv)
            self.assertIn("FIXTURE_ID=" + campaign.fixture_id, argv)
            env = invoke.call_args.kwargs["env"]
            self.assertEqual(env["DOCKER_CONFIG"], folder)
            self.assertNotIn("DOCKER_HOST", env)
            self.assertNotIn("COMPOSE_FILE", env)

    def test_fixture_network_identity_and_live_service_are_required(self):
        instance = descriptor_for("conix-abcdef123456")
        docker = FakeDocker()
        with patch.dict(os.environ, {"CONIX_TEST_NETWORK_INSPECT":
            "true " + instance["composeProject"] + " " + instance["dockerNetwork"]}):
            runner.verify_network(docker, instance)
        with patch.dict(os.environ, {"CONIX_TEST_NETWORK_INSPECT":
            "false " + instance["composeProject"] + " " + instance["dockerNetwork"]}):
            with self.assertRaises(runner.CampaignError):
                runner.verify_network(docker, instance)
        with patch.dict(os.environ, {"CONIX_TEST_NETWORK_INSPECT":
            "true unowned " + instance["dockerNetwork"]}):
            with self.assertRaises(runner.CampaignError):
                runner.verify_network(docker, instance)

    def test_admit_rejects_privilege_drift_and_owner_credential_leak(self):
        runtime = "sha256:" + "a" * 64
        postgres = "sha256:" + "b" * 64
        baseline = graph(runtime, postgres, "test-rpc")
        runner.admit(baseline, runner_image=runtime, postgres_image=postgres, rpc_network="test-rpc")
        for change in (
            lambda d: d["services"]["consumer"].update({"cap_add": ["SYS_ADMIN"]}),
            lambda d: d["services"]["consumer"]["environment"].update({
                "CONIX_TEST_OWNER_DSN": "should-not-be-here",
            }),
            lambda d: d["services"]["consumer"]["environment"].pop("CONIX_TEST_READ_DSN"),
            lambda d: d["services"]["initialize"]["environment"].update({
                "CONIX_TEST_READ_DSN": "must-not-reach-owner",
            }),
            lambda d: d["services"]["initialize"]["networks"].update({"rpc": None}),
            lambda d: d["services"]["consumer"].update({"build": {"context": ".."}}),
            lambda d: d["networks"]["rpc"].update({"name": "foreign"}),
            lambda d: d["services"]["consumer"].update({"ports": ["5432:5432"]}),
        ):
            candidate = json.loads(json.dumps(baseline))
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(runner.CampaignError):
                runner.admit(candidate, runner_image=runtime, postgres_image=postgres, rpc_network="test-rpc")

    def test_newly_created_fixture_is_rechecked_by_info_and_network(self):
        with tempfile.TemporaryDirectory() as path:
            docker = FakeDocker()
            campaign = runner.Campaign(
                docker, "science", Path(path), "sha256:" + "a" * 64, Path(path)
            )
            document = descriptor_for(campaign.fixture_id)
            body = (json.dumps(document) + "\n").encode()
            actions = []
            def producer(action):
                actions.append(action)
                return body
            with patch.object(campaign, "_dapp32", side_effect=producer), patch.dict(
                os.environ,
                {"CONIX_TEST_NETWORK_INSPECT": "true " + document["composeProject"] + " " + document["dockerNetwork"]},
            ):
                campaign.acquire()
            self.assertEqual(actions, ["up", "info"])
            self.assertTrue(campaign.producer_owned)
            self.assertEqual(campaign.fixture_descriptor, document)

    def test_wrong_descriptor_does_not_grant_cleanup_ownership(self):
        with tempfile.TemporaryDirectory() as path:
            campaign = runner.Campaign(
                FakeDocker(), "science", Path(path), "sha256:" + "a" * 64, Path(path)
            )
            document = descriptor_for(campaign.fixture_id)
            document["schema"] = "unexpected"
            with patch.object(campaign, "_dapp32", return_value=(json.dumps(document)+"\n").encode()):
                with self.assertRaises(FixtureError):
                    campaign.acquire()
            self.assertFalse(campaign.producer_owned)
            self.assertIsNone(campaign.fixture_descriptor)

    def test_project_name_is_not_sufficient_cleanup_ownership(self):
        with tempfile.TemporaryDirectory() as path:
            docker = FakeDocker()
            borrowed = runner.PayToPlayTest(
                docker, "sha256:" + "a" * 64, Path(path),
                descriptor_for("borrowed-1"),
            )
            calls = []
            def occupied(*args, **kwargs):
                calls.append(args)
                return "existing-id\n" if args[0] == "volume" else ""
            with patch.object(docker, "run", side_effect=occupied):
                with self.assertRaises(runner.CampaignError):
                    borrowed.check_new_sql_project()
            self.assertEqual({call[0] for call in calls}, {"container", "network", "volume"})
            self.assertFalse(any("down" in call for call in calls))

    def test_cleanup_order_respects_consumer_retirement(self):
        # Producer retirement must be causally downstream of consumer cleanup;
        # a matching Compose project label alone never authorizes deletion.
        with tempfile.TemporaryDirectory() as path:
            docker = FakeDocker()
            campaign = runner.Campaign(
                docker, "agentic", Path(path), "sha256:" + "a" * 64, Path(path),
            )
            campaign.producer_owned = True
            campaign.fixture_descriptor = descriptor_for(campaign.fixture_id)
            borrowed = runner.PayToPlayTest(
                docker, campaign.runner_image, Path(path), campaign.fixture_descriptor,
            )
            borrowed.sql_started = True
            borrowed.sql_retired = False
            borrowed.compose_environment = {}
            campaign.consumer_test = borrowed
            down = []
            def producer(action):
                down.append(action)
                self.assertTrue(borrowed.sql_retired)
                return b""
            with patch.object(campaign, "_dapp32", side_effect=producer):
                self.assertEqual(campaign.cleanup(), [])
            self.assertEqual(down, ["down"])
            self.assertTrue(any("down" in args for args in docker.calls))

    def test_unretired_consumer_prevents_deletion_of_producer_fixture(self):
        with tempfile.TemporaryDirectory() as path:
            docker = FakeDocker()
            campaign = runner.Campaign(
                docker, "agentic", Path(path), "sha256:" + "a" * 64, Path(path)
            )
            campaign.producer_owned = True
            campaign.fixture_descriptor = descriptor_for(campaign.fixture_id)
            borrowed = runner.PayToPlayTest(
                docker, campaign.runner_image, Path(path), campaign.fixture_descriptor,
            )
            borrowed.sql_started = True
            borrowed.sql_retired = False
            borrowed.compose_environment = {}
            campaign.consumer_test = borrowed
            with patch.object(docker, "run", side_effect=runner.OperationError("uncertain")), patch.object(
                campaign, "_dapp32"
            ) as producer:
                failures = campaign.cleanup()
            self.assertTrue(failures)
            self.assertFalse(borrowed.sql_retired)
            producer.assert_not_called()

    def test_borrowed_descriptor_is_private_and_does_not_transfer_cleanup_rights(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            descriptor = descriptor_for("borrowed-1")
            path.write_text(json.dumps(descriptor) + "\n")
            path.chmod(0o600)
            self.assertEqual(runner.read_borrowed_descriptor(str(path)), descriptor)
            path.chmod(0o644)
            with self.assertRaises(runner.CampaignError):
                runner.read_borrowed_descriptor(str(path))
            path.chmod(0o600)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(runner.CampaignError):
                runner.read_borrowed_descriptor(str(link))
            with self.assertRaises(runner.CampaignError):
                runner.read_borrowed_descriptor("relative.json")

    def test_borrowed_generic_acceptance_never_calls_producer_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = FakeDocker()
            descriptor = descriptor_for("borrowed-1")
            run = runner.PayToPlayTest(
                docker, "sha256:" + "a" * 64, Path(directory), descriptor,
            )
            run.preflight_record = {
                "schema": "conix.acceptance.preflight.v1",
                "chainId": 31337, "genesisHash": "0x" + "ab" * 32,
            }
            with patch.object(runner, "verify_network") as validate, patch.object(
                run, "prepare_sql"
            ), patch.object(run, "_phase") as phase:
                run.execute()
            validate.assert_called_once_with(docker, descriptor)
            phase.assert_called_once_with(
                "consumer", "baseline", prior_receipt=run.preflight_record,
                expect_receipt=False,
            )
            with patch.object(docker, "run", side_effect=AssertionError("Borrowed test contacted producer")):
                self.assertEqual(run.cleanup(), [])

    def test_only_owned_fixture_runs_mutating_t0_5_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = runner.PayToPlayTest(
                FakeDocker(), "sha256:" + "a" * 64, Path(directory),
                descriptor_for("conix-abcdef123456"), owned_fixture=True,
            )
            owned.preflight_record = {
                "schema": "conix.acceptance.preflight.v1",
                "chainId": 31337, "genesisHash": "0x" + "ab" * 32,
            }
            public_payment_receipt = {"schema": "public-source-only"}
            observed = []

            def phase(service, name, *, prior_receipt, expect_receipt):
                observed.append((service, name, prior_receipt, expect_receipt))
                if name == "exercise":
                    return public_payment_receipt
                return None

            with patch.object(runner, "verify_network"), patch.object(
                owned, "prepare_sql"
            ), patch.object(owned, "_phase", side_effect=phase):
                owned.execute()

            self.assertEqual([entry[1] for entry in observed],
                             ["baseline", "exercise", "resume", "reorg"])
            self.assertEqual(observed[0][2], owned.preflight_record)
            self.assertEqual(observed[1][2], owned.preflight_record)
            self.assertIs(observed[2][2], public_payment_receipt)
            self.assertIs(observed[3][2], public_payment_receipt)
            self.assertTrue(observed[1][3])
            self.assertFalse(observed[2][3] or observed[3][3])


    def test_token_deployment_precedes_schema_owner_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = FakeDocker()
            docker.require_role = lambda *_: None
            docker.require_image = lambda *_: None
            fixture = runner.PayToPlayTest(
                docker, "sha256:" + "a" * 64, Path(directory),
                descriptor_for("conix-abcdef123456"), owned_fixture=True,
            )
            preflight = {"schema": "conix.acceptance.preflight.v1", "chainId": 31337,
                         "genesisHash": "0x" + "ab" * 32}
            tokens = {"schema": "conix.acceptance.tokens.v1"}
            seen = []

            def phase(_service, operation, *, prior_receipt, expect_receipt):
                seen.append((operation, prior_receipt, expect_receipt))
                if operation == "preflight":
                    return preflight
                if operation == "prepare":
                    return tokens
                if operation == "initialize":
                    self.assertIs(fixture.tokens, tokens)
                    self.assertIs(prior_receipt, preflight)

            with patch.object(runner, "pinned_images", return_value={
                "postgres": "test-postgres",
            }), patch.object(runner, "admit"), patch.object(
                fixture, "check_new_sql_project"
            ), patch.object(fixture, "_phase", side_effect=phase), patch.object(
                docker, "run", return_value="{}"
            ):
                fixture.prepare_sql()

            self.assertEqual([operation for operation, _, _ in seen],
                             ["bootstrap", "preflight", "prepare", "initialize"])
            self.assertIs(seen[2][1], preflight)
            self.assertTrue(seen[2][2])
            self.assertFalse(seen[3][2])

    def test_unconfirmed_producer_creation_does_not_grant_cleanup_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = runner.Campaign(
                FakeDocker(), "agentic", Path(directory),
                "sha256:" + "a" * 64, Path(directory),
            )
            campaign.creation_attempted = True
            campaign.fixture_descriptor = None
            campaign.producer_owned = False
            with patch.object(campaign, "_dapp32") as producer:
                self.assertTrue(campaign.cleanup())
            producer.assert_not_called()

    def test_borrowed_make_action_never_uses_producer_or_context_reselection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.json"
            path.write_text(json.dumps(descriptor_for("exclusive-1")) + "\n")
            path.chmod(0o600)
            image = "sha256:" + "a" * 64
            arguments = [
                "pay_to_play", "--origin", "command line",
                "--context", "agentic", "test/indexer/acceptance/run",
                "--runner-image", image, "--fixture-descriptor", str(path),
            ]
            with patch.object(sys, "argv", arguments), patch.object(
                runner, "select_endpoint", return_value="unix:///tmp/selected.sock"
            ), patch.object(runner, "Docker") as engine, patch.object(
                runner, "sibling_checkout", side_effect=AssertionError("Borrowed test looked up dapp32")
            ), patch.object(
                runner, "pin_sibling_context", side_effect=AssertionError("Borrowed test touched producer Docker context")
            ), patch.object(
                runner, "Campaign", side_effect=AssertionError("Borrowed test created a campaign")
            ), patch.object(
                runner.PayToPlayTest, "execute"
            ) as execute, patch.object(
                runner.PayToPlayTest, "cleanup", return_value=[]
            ) as cleanup, patch.object(runner.signal, "signal"):
                runner.main()
            engine.return_value.require_rootless_engine.assert_called_once()
            engine.return_value.require_role.assert_called_once_with(image, "evm-test-runner")
            execute.assert_called_once()
            cleanup.assert_called_once()

    def test_failed_fixture_phase_reports_its_verified_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = runner.PayToPlayTest(
                FakeDocker(), "sha256:" + "a" * 64, Path(directory),
                descriptor_for("conix-abcdef123456"),
            )
            fixture.compose_environment = {}
            fixture.docker.docker_command_prefix = ["docker"]

            def failed(_argv, *, error_file, **_kwargs):
                error_file.write_bytes(phase_failure_record(
                    "baseline", ScenarioFailure("Generic acceptance differs from real evidence")
                ) + b"\n")
                raise runner.CampaignError("Conix phase/baseline exited with status 1.")

            with patch.object(runner, "owned_command", side_effect=failed):
                with self.assertRaises(runner.CampaignError) as raised:
                    fixture._phase("consumer", "baseline", prior_receipt=None, expect_receipt=False)
            self.assertIn("phase/baseline", str(raised.exception))
            self.assertIn("Generic acceptance differs", str(raised.exception))
            self.assertFalse(fixture.consumer_retired)

    def test_failed_subprocess_keeps_private_output_out_of_terminal(self):
        """The final error may name a phase/status, never child wallet output."""
        with tempfile.TemporaryDirectory() as directory:
            secret = "0x" + "ab" * 32
            output = Path(directory) / "out"
            stderr = Path(directory) / "err"
            terminal = io.StringIO()
            with redirect_stderr(terminal):
                with self.assertRaises(runner.CampaignError) as raised:
                    runner.owned_command(
                        [sys.executable, "-c",
                         "import sys; print(sys.argv[1]); print(sys.argv[1], file=sys.stderr); sys.exit(7)",
                         secret],
                        output=output, error_file=stderr,
                        env=os.environ.copy(), timeout=5, label="scenario/checked",
                    )
            self.assertIn("scenario/checked", str(raised.exception))
            self.assertIn("status 7", str(raised.exception))
            self.assertNotIn(secret, terminal.getvalue() + str(raised.exception))
            self.assertIn(secret.encode(), stderr.read_bytes())

    def test_cleanup_exception_does_not_erase_the_first_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            descriptor = Path(directory) / "fixture.json"
            descriptor.write_text(json.dumps(descriptor_for("borrowed-1")) + "\n")
            descriptor.chmod(0o600)
            image = "sha256:" + "a" * 64
            args = [
                "pay_to_play", "--origin", "command line",
                "--context", "agentic", "test/indexer/acceptance/run",
                "--runner-image", image, "--fixture-descriptor", str(descriptor),
            ]
            terminal = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(
                runner, "select_endpoint", return_value="unix:///tmp/selected.sock"
            ), patch.object(runner, "Docker") as docker, patch.object(
                runner.PayToPlayTest, "execute",
                side_effect=runner.CampaignError("Primary acceptance assertion failed.")
            ), patch.object(
                runner.PayToPlayTest, "cleanup",
                side_effect=RuntimeError("cleanup function broke")
            ), patch.object(runner.signal, "signal"), redirect_stderr(terminal):
                docker.return_value.endpoint = "unix:///tmp/selected.sock"
                with self.assertRaises(SystemExit) as raised:
                    runner.main()
            result = str(raised.exception)
            self.assertIn("Primary acceptance assertion failed", result)
            self.assertIn("cleanup", result.lower())
            output = terminal.getvalue()
            self.assertIn("Cleanup unconfirmed", output)
            self.assertIn("Recovery:", output)
            records = [line for line in output.splitlines() if line.startswith("Private phase receipts: ")]
            self.assertEqual(len(records), 1)
            shutil.rmtree(records[0].removeprefix("Private phase receipts: "))

    def test_unreapable_child_does_not_replace_primary_operation_failure(self):
        """Process-retirement failure must not erase the first causal error."""
        with tempfile.TemporaryDirectory() as directory:
            class StuckChild:
                pid = 73159
                returncode = None

                def communicate(self, **_kwargs):
                    raise RuntimeError("original operation failed")

                def wait(self, *, timeout):
                    raise runner.subprocess.TimeoutExpired("test-child", timeout)

            killed = []
            with patch.object(
                runner.subprocess, "Popen", return_value=StuckChild()
            ), patch.object(
                runner.os, "killpg", side_effect=lambda pid, signal: killed.append((pid, signal))
            ):
                with self.assertRaisesRegex(RuntimeError, "original operation failed") as raised:
                    runner.owned_command(
                        ["test-child"],
                        output=Path(directory) / "stdout",
                        error_file=Path(directory) / "stderr",
                        env={},
                        timeout=1,
                    )

            self.assertEqual(len(killed), 2)
            self.assertIn("retirement", " ".join(raised.exception.__notes__).lower())

    def test_timed_out_owned_process_is_reaped_before_retirement(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "stdout"
            error = Path(directory) / "stderr"
            start = time.monotonic()
            with self.assertRaises(runner.CampaignError):
                runner.owned_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    output=output, error_file=error, env=os.environ.copy(),
                    timeout=0.1,
                )
            self.assertLess(time.monotonic() - start, 5)
            self.assertTrue(output.is_file())
            self.assertTrue(error.is_file())

    def test_sibling_path_is_explicit_and_non_symlinked(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            for name in ("Makefile", "tools/pay_to_play_fixture.sh", "tools/pay_to_play_fixture.py"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("fixture")
            self.assertEqual(runner.sibling_checkout(str(root)), root)
            with self.assertRaises(runner.CampaignError):
                runner.sibling_checkout("../dapp32")
            link = root.parent / (root.name + "-link")
            link.symlink_to(root)
            try:
                with self.assertRaises(runner.CampaignError):
                    runner.sibling_checkout(str(link))
            finally:
                link.unlink()


if __name__ == "__main__":
    unittest.main()


class GenericAcceptanceOperatorContracts(unittest.TestCase):
    """Opt-in acceptance must use the same contained, owned fixture boundary."""

    def test_acceptance_entrypoint_overrides_only_the_test_program(self):
        with tempfile.TemporaryDirectory() as path:
            descriptor = descriptor_for("conix-abcdef123456")
            docker = SimpleNamespace(docker_command_prefix=("docker",), docker_environment={})
            consumer = runner.PayToPlayTest(
                docker, "sha256:" + "a" * 64, Path(path), descriptor ,
            )
            consumer.compose_environment = {}
            state = {
                "schema": "conix.acceptance.preflight.v1",
                "chainId": 31337, "genesisHash": "0x" + "ab" * 32,
            }
            with patch.object(runner, "owned_command", return_value=b"") as invoke:
                consumer._phase("consumer", "baseline", prior_receipt=state, expect_receipt=False)
            argv = invoke.call_args.args[0]
            self.assertIn("--entrypoint", argv)
            self.assertEqual(argv[argv.index("--entrypoint") + 1:], [
                "python", "consumer", "-I", "/tests/generic_acceptance.py", "baseline",
            ])
            self.assertNotIn("privateKey", str(argv))
            self.assertTrue(consumer.consumer_retired)

    def test_borrowed_acceptance_cannot_retire_a_producer(self):
        with tempfile.TemporaryDirectory() as path:
            consumer = runner.PayToPlayTest(
                FakeDocker(), "sha256:" + "a" * 64, Path(path),
                descriptor_for("conix-abcdef123456") ,
            )
            self.assertFalse(consumer.sql_started)
            self.assertEqual(consumer.cleanup(), [])


    def test_every_consumer_phase_receives_keyless_public_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            descriptor = descriptor_for("conix-abcdef123456")
            docker = SimpleNamespace(docker_command_prefix=("docker",), docker_environment={})
            consumer = runner.PayToPlayTest(
                docker, "sha256:" + "a" * 64, Path(directory),
                descriptor, owned_fixture=True,
            )
            consumer.compose_environment = {}

            for phase in ("preflight", "baseline", "exercise", "resume", "reorg"):
                with self.subTest(phase=phase), patch.object(
                    runner, "owned_command", return_value=b""
                ) as invoked:
                    consumer._phase(
                        "consumer", phase, prior_receipt=None, expect_receipt=False,
                    )

                payload = invoked.call_args.kwargs["payload"].decode("utf-8")
                source = json.loads(payload)["descriptor"]
                self.assertEqual(source["schema"], "conix.acceptance.public-test-source.v1")
                self.assertEqual(set(source["contracts"]), {"PayToPlay"})
                self.assertEqual(
                    source["publicFixtureAccounts"]["admin"],
                    {"address": descriptor["publicFixtureAccounts"]["admin"]["address"]},
                )
                self.assertNotIn("privateKey", payload)
                for account in descriptor["publicFixtureAccounts"].values():
                    self.assertNotIn(account["privateKey"], payload)

    def test_generic_owner_receives_no_private_fixture_descriptor(self):
        with tempfile.TemporaryDirectory() as path:
            descriptor = descriptor_for("conix-abcdef123456")
            docker = SimpleNamespace(docker_command_prefix=("docker",), docker_environment={})
            consumer = runner.PayToPlayTest(
                docker, "sha256:" + "a" * 64, Path(path), descriptor ,
            )
            consumer.compose_environment = {}
            genesis = {
                "schema": "conix.acceptance.preflight.v1",
                "chainId": 31337, "genesisHash": "0x" + "ab" * 32,
            }
            with patch.object(runner, "owned_command", return_value=b"") as invoked:
                consumer._phase("initialize", "initialize", prior_receipt=genesis, expect_receipt=False)
            payload = json.loads(invoked.call_args.kwargs["payload"].decode())
            self.assertEqual(payload["state"], genesis)
            self.assertEqual(
                set(payload["descriptor"]),
                {"schema", "chainId", "rpcUrl", "payment"},
            )
            self.assertEqual(
                set(payload["descriptor"]["payment"]),
                {"address", "deploymentBlock"},
            )
            self.assertNotIn("privateKey", invoked.call_args.kwargs["payload"].decode())
            self.assertNotIn("publicFixtureAccounts", invoked.call_args.kwargs["payload"].decode())
