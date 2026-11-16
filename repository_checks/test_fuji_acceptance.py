"""Live-source inputs and grants are explicit before an Engine is touched."""

import hashlib
import json
from pathlib import Path
import subprocess
import tomllib
import unittest

from tools.wheelhouse.policy import source_allowlist

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config/deployments/fuji-acceptance"


class FujiSourceContracts(unittest.TestCase):
    def test_deployed_abi_is_explicitly_streamed_into_the_test_image(self):
        """The real deployed ABI must not vanish from a closed build context."""
        inputs = {entry["destination"] for entry in source_allowlist(ROOT)}
        self.assertIn("config/deployments/fuji-acceptance/abi/pay_to_play.json", inputs)

    def test_public_make_lane_is_discoverable(self):
        """An operator can invoke one maintained external acceptance command."""
        result = subprocess.run(["make", "-s", "help"], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertIn("test/indexer/acceptance/fuji/run", result.stdout)

    def test_profile_is_bound_to_the_reviewed_full_deployment_abi(self):
        """Source identity and selection come from pinned public artifacts."""
        source = json.loads((ROOT / "tests/fixtures/evm/fuji_pay_to_play.json").read_text())
        profile = tomllib.loads((PROFILE / "deployment.toml").read_text())
        raw = (PROFILE / "abi/pay_to_play.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), source["abi_sha256"])
        events = {e["name"] + "(" + ",".join(a["type"] for a in e["inputs"]) + ")"
                  for e in json.loads(raw) if e.get("type") == "event"}
        self.assertEqual(len(events), 17)
        self.assertEqual(set(profile["contracts"]["payment"]["events"]), events)
        self.assertEqual(profile["contracts"]["payment"]["address"], source["address"])
        self.assertEqual(profile["chain"]["start_block"], source["initial_interval"]["start_block"])
        self.assertEqual(profile["chain"]["genesis_hash"], source["genesis_hash"])
        self.assertEqual(profile["chain"]["chain_id"], source["chain_id"])


class FujiAuthorityContracts(unittest.TestCase):
    def graph(self):
        from tools.fixtures.fuji import OWNER_LABEL, PROJECT
        services = {}
        for name in ("postgres", "bootstrap", "gateway"):
            services[name] = {
                "image": "sha256:" + ("a" if name == "gateway" else "b") * 64,
                "pull_policy": "never", "read_only": True,
                "networks": {key: {} for key in (("rpc", "egress") if name == "gateway" else ("tests",))},
                "user": "65532:65532" if name == "gateway" else "999:999",
                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "labels": {OWNER_LABEL: "owned"},
            }
        services["postgres"]["volumes"] = [{"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql/data"}]
        services["gateway"].update(entrypoint=["python", "-E", "-s", "/tests/fuji_rpc.py"], environment={"CONIX_FUJI_VIEW": "historical"})
        return {"services": services,
                "networks": {name: {"name": PROJECT + "_" + name, "internal": name != "egress", "labels": {OWNER_LABEL: "owned"}}
                             for name in ("tests", "rpc", "egress")},
                "volumes": {"pgdata": {"name": PROJECT + "_pgdata", "labels": {OWNER_LABEL: "owned"}}}}

    def test_exact_fixture_grants_and_rejection_of_extra_authority(self):
        from copy import deepcopy
        from tools.fixtures.fuji import admit_fixture
        from tools.containers.client import OperationError
        good = self.graph()
        arguments = ({"postgres": "sha256:" + "b" * 64}, "sha256:" + "a" * 64, "owned", "historical")
        admit_fixture(good, *arguments)
        mutations = [
            lambda g: g["services"]["postgres"]["networks"].update(egress={}),
            lambda g: g["services"]["gateway"].update(ports=[{"target": 8080, "published": "8080"}]),
            lambda g: g["services"]["gateway"].update(volumes=[{"type": "bind", "source": "/home"}]),
            lambda g: g["services"]["gateway"]["environment"].update(HTTP_PROXY="http://unselected"),
            lambda g: g["services"]["gateway"].update(user="0:0"),
            lambda g: g["services"]["gateway"].update(volumes=[{"type": "volume", "source": "pgdata", "target": "/data"}]),
            lambda g: g["services"]["postgres"]["volumes"][0].update(source="unselected"),
            lambda g: g["services"]["gateway"].update(secrets=[{"source": "unexpected"}]),
            lambda g: g["networks"]["rpc"].update(internal=False),
            lambda g: g["volumes"]["pgdata"].update(external=True),
            lambda g: g["volumes"]["pgdata"].update(name="unselected-existing-data"),
            lambda g: g["networks"]["rpc"].update(name="unselected-existing-network"),
            lambda g: g["networks"]["egress"].update(driver="host"),
        ]
        for mutate in mutations:
            graph = deepcopy(good)
            mutate(graph)
            with self.assertRaises(OperationError):
                admit_fixture(graph, *arguments)

    def test_consumer_retirement_failure_preserves_dependencies(self):
        from types import SimpleNamespace
        from tools.fixtures.fuji import FujiAcceptance
        campaign = FujiAcceptance.__new__(FujiAcceptance)
        campaign.fixture_attempted = True
        campaign.records = {"phase": "consumer retirement"}
        campaign.touched = ["fuji-acceptance"]
        campaign.docker = SimpleNamespace()
        called = []

        def fail(action, name):
            called.append((action, name))
            raise OSError("private credential must not appear")

        campaign.make = fail
        campaign.child = lambda *_args, **_kwargs: self.fail("Dependencies retired after uncertain consumer.")
        failures = campaign.cleanup()
        self.assertEqual(called, [("deployment/down", "fuji-acceptance")])
        self.assertEqual(len(failures), 1)
        self.assertIn("fuji-acceptance", failures[0])
        self.assertIn("cause=OSError", failures[0])
        self.assertNotIn("private credential", failures[0])

    def test_first_failure_phase_survives_cleanup_and_hostile_exception_text(self):
        import tempfile
        from types import SimpleNamespace
        from tools.fixtures.fuji import finish_acceptance
        from tools.containers.client import OperationError

        class PrivateFailure(RuntimeError):
            def __str__(self):
                raise AssertionError("Unexpected values must not be stringified.")

        failure = PrivateFailure()
        with tempfile.TemporaryDirectory() as directory:
            campaign = SimpleNamespace(evidence=Path(directory), records={"phase": "source check"})

            def cleanup():
                campaign.records["phase"] = "fixture retirement"
                raise PermissionError("private")

            campaign.cleanup = cleanup
            with self.assertRaises(OperationError) as caught:
                finish_acceptance(campaign, failure, "source check")
            self.assertIs(caught.exception.__cause__, failure)
            self.assertIn("source check", str(caught.exception))
            self.assertNotIn("private", str(caught.exception))
            receipt = json.loads((Path(directory) / "result.json").read_text())
            self.assertFalse(receipt["passed"])
            self.assertEqual(receipt["primary_failure"]["phase"], "source check")
            self.assertEqual(len(receipt["cleanup_errors"]), 1)
            self.assertIn("cause=PermissionError", receipt["cleanup_errors"][0])

    def test_file_newline_is_not_part_of_the_reader_database_name(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fixtures.fuji import FujiAcceptance
        campaign = FujiAcceptance.__new__(FujiAcceptance)
        campaign.definition = {"rpc_network": "rpc", "database_network": "db"}
        campaign.runner = "sha256:" + "a" * 64
        campaign.owner = "owned"
        campaign.docker = SimpleNamespace(
            docker_command_prefix=["docker"], private_bytes=lambda *_args, **_kwargs: b"postgresql://fixture/db\n",
        )
        captured = []

        def child(_argv, _label, *, payload, timeout):
            captured.append(json.loads(payload))
            return b"{}"

        campaign.child = child
        with patch("tools.fixtures.fuji._admit_network"):
            campaign.probe("snapshot")
        self.assertEqual(captured, [{"operation": "snapshot", "dsn": "postgresql://fixture/db"}])

    def test_receipt_publication_failure_does_not_replace_primary_or_cleanup(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fixtures import fuji
        from tools.containers.client import OperationError
        primary = OperationError("Original source check failed.")
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "not-a-directory"
            evidence.write_text("unavailable evidence directory")
            campaign = SimpleNamespace(evidence=evidence, records={}, cleanup=lambda: ["Reader retirement unconfirmed"])
            with patch.object(fuji, "progress") as emitted:
                with self.assertRaises(OperationError) as caught:
                    fuji.finish_acceptance(campaign, primary, "source check")
            self.assertIs(caught.exception.__cause__, primary)
            self.assertTrue(any("Reader retirement" in str(call) for call in emitted.call_args_list))
            self.assertTrue(any("evidence publication" in str(call) for call in emitted.call_args_list))

    def test_broken_cleanup_diagnostic_stream_does_not_replace_primary(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fixtures import fuji
        from tools.containers.client import OperationError
        primary = OperationError("Original source check failed.")
        with tempfile.TemporaryDirectory() as directory:
            campaign = SimpleNamespace(evidence=Path(directory), records={}, cleanup=lambda: ["Reader retirement unconfirmed"])
            with patch.object(fuji, "progress", side_effect=BrokenPipeError("private output error")):
                with self.assertRaises(OperationError) as caught:
                    fuji.finish_acceptance(campaign, primary, "source check")
            self.assertIs(caught.exception.__cause__, primary)


    def test_receipt_replacement_failure_preserves_prior_private_evidence(self):
        import tempfile
        from unittest.mock import patch
        from tools.fixtures.fuji import publish_result
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            publish_result(folder, {"passed": False, "owner": "earlier"})
            with patch("tools.fixtures.fuji.os.replace", side_effect=OSError("private")):
                with self.assertRaises(OSError):
                    publish_result(folder, {"passed": True})
            self.assertEqual(json.loads((folder / "result.json").read_text())["owner"], "earlier")
            self.assertEqual((folder / "result.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(folder.iterdir()), [folder / "result.json"])

    def test_named_child_retains_one_admitted_record_and_suppresses_prose(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fixtures.fuji import FujiAcceptance
        from tools.fixtures.process import CampaignError
        from tools.containers.client import OperationError
        from tools.containers.diagnostics import PREFIX, admitted
        record = PREFIX + json.dumps({"phase": "reader_check", "cause": "StorageUnavailable", "effect": "unconfirmed"}).encode()
        with tempfile.TemporaryDirectory() as directory:
            campaign = FujiAcceptance.__new__(FujiAcceptance)
            campaign.definition = {"docker_context": "agentic"}
            campaign.docker = SimpleNamespace(endpoint="selected")
            campaign.evidence = Path(directory)
            campaign.step = 1
            campaign.child = lambda *_a, **_kw: (_ for _ in ()).throw(CampaignError("Child failed.", exit_code=2))
            with patch("tools.fixtures.fuji.select_endpoint", return_value="selected"):
                for copies in (1, 2):
                    (campaign.evidence / "001.err").write_bytes(b"private credential\n" + (record + b"\n") * copies)
                    with self.assertRaises(OperationError if copies == 1 else CampaignError) as caught:
                        campaign.make("deployment/reader/check")
                    self.assertNotIn("private credential", str(caught.exception))
                    if copies == 1:
                        self.assertIn("reader check", str(caught.exception))
                        self.assertEqual(admitted(caught.exception.diagnostic)["cause"], "StorageUnavailable")

    def test_operator_failure_record_never_stringifies_exception_values(self):
        from tools.containers.diagnostics import admitted, error_record
        class HostileFailure(RuntimeError):
            def __str__(self):
                raise AssertionError("Do not stringify private values.")
        try:
            raise HostileFailure()
        except HostileFailure as error:
            record = admitted(error_record(error))
        self.assertEqual(record["cause"], "HostileFailure")
        self.assertEqual(record["phase"], "operator")
        self.assertIn("test_fuji_acceptance.py:", record["location"])


    def test_missing_child_receipt_does_not_replace_failed_make(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fixtures.fuji import FujiAcceptance
        from tools.fixtures.process import CampaignError
        original = CampaignError("Named operation failed.", exit_code=2)
        with tempfile.TemporaryDirectory() as directory:
            campaign = FujiAcceptance.__new__(FujiAcceptance)
            campaign.definition = {"docker_context": "agentic"}
            campaign.docker = SimpleNamespace(endpoint="selected")
            campaign.evidence = Path(directory)
            campaign.step = 1
            campaign.child = lambda *_a, **_kw: (_ for _ in ()).throw(original)
            with patch("tools.fixtures.fuji.select_endpoint", return_value="selected"):
                with self.assertRaises(CampaignError) as caught:
                    campaign.make("deployment/up")
            self.assertIs(caught.exception, original)

    def test_final_failure_retains_the_original_producer_record(self):
        import tempfile
        from types import SimpleNamespace
        from tools.fixtures.fuji import finish_acceptance
        from tools.containers.client import OperationError
        from tools.containers.diagnostics import PREFIX, admitted, error_record
        record = PREFIX + json.dumps({"phase": "reader_check", "cause": "StorageUnavailable", "effect": "unconfirmed"}).encode()
        primary = OperationError("Reader check failed.", diagnostic=record, exit_code=2)
        with tempfile.TemporaryDirectory() as directory:
            campaign = SimpleNamespace(evidence=Path(directory), records={}, cleanup=lambda: [])
            with self.assertRaises(OperationError) as caught:
                finish_acceptance(campaign, primary, "reader check")
        self.assertEqual(admitted(error_record(caught.exception)), dict(admitted(record), child_exit=2))
        self.assertEqual(caught.exception.exit_code, 2)
