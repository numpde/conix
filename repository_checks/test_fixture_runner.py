"""The sibling fixture runner's ownership and admission contract, no Docker.

Exercises failures before acquisition, descriptor/endpoint provenance, rendered
privileges and teardown order without inventing a fake indexer or signed logs.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import fixture as runner
from fixture_protocol import FixtureError
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
        runner.admit(baseline, fixture_image=runtime, postgres_image=postgres, rpc_network="test-rpc")
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
                runner.admit(candidate, fixture_image=runtime, postgres_image=postgres, rpc_network="test-rpc")

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
            campaign = runner.Campaign(
                FakeDocker(), "science", Path(path), "sha256:" + "a" * 64, Path(path)
            )
            calls = []
            def occupied(*args, **kwargs):
                calls.append(args)
                return "existing-id\n" if args[0] == "volume" else ""
            with patch.object(campaign.docker, "run", side_effect=occupied):
                with self.assertRaises(runner.CampaignError):
                    campaign.check_new_sql_project()
            self.assertTrue(any(call[0] == "container" for call in calls))
            self.assertTrue(any(call[0] == "network" for call in calls))
            self.assertTrue(any(call[0] == "volume" for call in calls))
            self.assertFalse(any("down" in call for call in calls))

    def test_cleanup_order_respects_consumer_retirement(self):
        with tempfile.TemporaryDirectory() as path:
            docker = FakeDocker()
            campaign = runner.Campaign(
                docker, "science", Path(path), "sha256:" + "a" * 64, Path(path)
            )
            campaign.sql_started = True
            campaign.sql_retired = False
            campaign.producer_owned = True
            campaign.descriptor = descriptor_for(campaign.fixture_id)
            campaign.env = {}
            down = []
            def producer(action):
                down.append(action)
                self.assertTrue(campaign.sql_retired)
                return b""
            with patch.object(campaign, "_dapp32", side_effect=producer):
                self.assertEqual(campaign.cleanup(), [])
            self.assertEqual(down, ["down"])
            self.assertTrue(any("down" in args for args in docker.calls))

    def test_unretired_consumer_prevents_deletion_of_producer_fixture(self):
        with tempfile.TemporaryDirectory() as path:
            docker = FakeDocker()
            campaign = runner.Campaign(
                docker, "science", Path(path), "sha256:" + "a" * 64, Path(path)
            )
            campaign.sql_started = True
            campaign.sql_retired = False
            campaign.producer_owned = True
            campaign.descriptor = descriptor_for(campaign.fixture_id)
            campaign.env = {}
            with patch.object(docker, "run", side_effect=runner.OperationError("uncertain")), patch.object(
                campaign, "_dapp32"
            ) as producer:
                failures = campaign.cleanup()
            self.assertTrue(failures)
            self.assertFalse(campaign.sql_retired)
            producer.assert_not_called()

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
