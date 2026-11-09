"""The sibling fixture runner's ownership and admission contract, no Docker.

Exercises failures before acquisition, descriptor/endpoint provenance, rendered
privileges and teardown order without inventing a fake indexer or signed logs.
"""
import io
import json
import os
import re
import shutil
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from tools.fixtures import pay_to_play as runner
from fixture_protocol import FixtureError, ScenarioFailure, phase_failure_record
from test_fixture_protocol import valid


def descriptor_for(fixture_id):
    document = valid()
    project = "dapps-pay-to-play-fixture-" + fixture_id
    document["fixtureId"] = fixture_id
    document["composeProject"] = project
    document["dockerNetwork"] = project + "_pay-to-play_local"
    return document


def graph(fixture_image, postgres_image, rpc):
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
            }, {"CONIX_TEST_DSN": "test-only:runtime"}),
        },
        "networks": {"database": {"internal": True},
                     "rpc": {"external": True, "name": rpc}},
    }


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.env = {}

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("network", "inspect"):
            return os.environ.get("CONIX_TEST_NETWORK_INSPECT", "")
        if args and args[0] == "ps":
            return "0123456789ab\n"
        return None


class FixtureRunnerTests(unittest.TestCase):
    def test_sibling_context_is_frozen_in_private_docker_client(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = SimpleNamespace(
                endpoint="unix:///tmp/selected-engine.sock",
                config_dir=Path(folder),
                env={"DOCKER_CONFIG": folder, "COMPOSE_DISABLE_ENV_FILE": "1"},
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
            for argv, kwargs in commands:
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
            self.assertEqual(campaign.descriptor, document)

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
            self.assertIsNone(campaign.descriptor)

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
        with tempfile.TemporaryDirectory() as path:
            docker = FakeDocker()
            campaign = runner.Campaign(
                docker, "agentic", Path(path), "sha256:" + "a" * 64, Path(path),
            )
            campaign.producer_owned = True
            campaign.descriptor = descriptor_for(campaign.fixture_id)
            borrowed = runner.PayToPlayTest(
                docker, campaign.runner_image, Path(path), campaign.descriptor,
            )
            borrowed.sql_started = True
            borrowed.sql_retired = False
            borrowed.env = {}
            campaign.test = borrowed
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
            campaign.descriptor = descriptor_for(campaign.fixture_id)
            borrowed = runner.PayToPlayTest(
                docker, campaign.runner_image, Path(path), campaign.descriptor,
            )
            borrowed.sql_started = True
            borrowed.sql_retired = False
            borrowed.env = {}
            campaign.test = borrowed
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

    def test_borrowed_executable_never_calls_dapp32_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = FakeDocker()
            descriptor = descriptor_for("borrowed-1")
            run = runner.PayToPlayTest(docker, "sha256:" + "a" * 64,
                                       Path(directory), descriptor)
            count = {"baseline": 0, "collected": 2, "restart": 2, "reorg": 0}
            phases = []
            def phase(_service, name, *, state, need_receipt):
                phases.append((name, state["phase"] if state else None))
                return {"phase": name, "collection_count": count[name]}
            with patch.object(runner, "verify_network") as validate, patch.object(
                run, "sql"
            ), patch.object(run, "_phase", side_effect=phase):
                run.execute()
            validate.assert_called_once_with(docker, descriptor)
            self.assertEqual(phases, [
                ("baseline", None), ("collected", "baseline"),
                ("restart", "collected"), ("reorg", "restart"),
            ])
            with patch.object(docker, "run", side_effect=AssertionError("Should not contact producer")):
                self.assertEqual(run.cleanup(), [])

    def test_unconfirmed_producer_creation_does_not_grant_cleanup_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = runner.Campaign(
                FakeDocker(), "agentic", Path(directory),
                "sha256:" + "a" * 64, Path(directory),
            )
            campaign.creation_attempted = True
            campaign.descriptor = None
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
                "--context", "agentic", "test/pay-to-play/run",
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
            fixture.env = {}
            fixture.docker.base = ["docker"]

            def failed(_argv, *, error_file, **_kwargs):
                error_file.write_bytes(phase_failure_record(
                    "collected", ScenarioFailure("Indexed collection differs from real receipt")
                ) + b"\n")
                raise runner.CampaignError("Conix phase/collected exited with status 1.")

            with patch.object(runner, "owned_command", side_effect=failed):
                with self.assertRaises(runner.CampaignError) as raised:
                    fixture._phase("consumer", "collected", state=None, need_receipt=True)
            self.assertIn("phase/collected", str(raised.exception))
            self.assertIn("Indexed collection differs", str(raised.exception))
            self.assertFalse(fixture.consumer_retired)

    def test_failed_subprocess_keeps_private_output_out_of_terminal(self):
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
                "--context", "agentic", "test/pay-to-play/run",
                "--runner-image", image, "--fixture-descriptor", str(descriptor),
            ]
            terminal = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(
                runner, "select_endpoint", return_value="unix:///tmp/selected.sock"
            ), patch.object(runner, "Docker") as docker, patch.object(
                runner.PayToPlayTest, "execute",
                side_effect=runner.CampaignError("Primary collection assertion failed.")
            ), patch.object(
                runner.PayToPlayTest, "cleanup",
                side_effect=RuntimeError("cleanup function broke")
            ), patch.object(runner.signal, "signal"), redirect_stderr(terminal):
                docker.return_value.endpoint = "unix:///tmp/selected.sock"
                with self.assertRaises(SystemExit) as raised:
                    runner.main()
            result = str(raised.exception)
            self.assertIn("Primary collection assertion failed", result)
            self.assertIn("cleanup", result.lower())
            output = terminal.getvalue()
            self.assertIn("Cleanup unconfirmed", output)
            self.assertIn("Recovery:", output)
            match = re.search(r"Private phase receipts: ([^\\n]+)", output)
            self.assertIsNotNone(match)
            shutil.rmtree(match.group(1))

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
