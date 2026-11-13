"""Host contracts for the named deployment acceptance lane; no Docker required."""

import copy
import json
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.containers.client import OperationError
from tools.fixtures import deployment as lane
from tools.fixtures.process import cancellation


class DeploymentAcceptanceContracts(unittest.TestCase):
    """Prove refusal, normal lifecycle delegation and cleanup ownership."""

    def campaign(self, folder):
        """Supply an empty selected Engine without weakening real preflight checks."""
        decl = SimpleNamespace(
            docker_context="science", wheelhouse="/wheels",
            database_network=lane.PROJECT + "_tests", rpc_network=lane.PROJECT + "_rpc",
        )
        docker = Mock()
        docker.endpoint = "unix:///tmp/selected.sock"
        docker.docker_command_prefix = ["docker", "--host", docker.endpoint]
        docker.docker_environment = {}
        docker.run.return_value = ""
        return lane.DeploymentAcceptance(decl, docker, {}, folder)

    def test_existing_lock_refuses_before_engine_effects(self):
        """A leftover prepared selection is recovery state, not a fresh fixture."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            campaign = self.campaign(folder)
            with patch.object(lane.named, "inspect_prepared", return_value=SimpleNamespace(
                image="sha256:" + "a" * 64, volume="conix-inputs-" + "b" * 24,
            )):
                with self.assertRaises(OperationError):
                    campaign.preflight()
            campaign.docker.run.assert_not_called()

    def test_existing_reader_snapshot_refuses_new_fixture_before_engine_work(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            with (
                patch.object(lane.named, "inspect_prepared", return_value=None),
                patch.object(lane.named, "inspect_reader_prepared", return_value=SimpleNamespace(
                    image="sha256:" + "a" * 64,
                    volume="conix-inputs-" + "b" * 24,
                )),
            ):
                with self.assertRaises(OperationError):
                    campaign.preflight()
            campaign.docker.run.assert_not_called()

    def test_existing_indexer_is_not_adopted_or_retired(self):
        """The stable deployment ID must not authorize touching an earlier service."""
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.docker.run.return_value = "a" * 64
            with (
                patch.object(lane.named, "inspect_prepared", return_value=None),
                patch.object(lane.named, "inspect_reader_prepared", return_value=None),
            ):
                with self.assertRaises(OperationError):
                    campaign.preflight()
            self.assertFalse(campaign.fixture_attempted)
            self.assertEqual(campaign.cleanup(), [])
            campaign.docker.remove_inputs.assert_not_called()

    def test_boot_uses_public_make_with_saved_selection(self):
        """Acceptance must exercise deployment/up rather than a second startup path."""
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.child = Mock(return_value=b"")
            with patch.object(lane, "select_endpoint", return_value=campaign.docker.endpoint):
                campaign.make("deployment/up")
            argv = campaign.child.call_args.args[0]
            self.assertEqual(argv, ["make", "--no-print-directory", "deployment/up",
                                    "DEPLOYMENT=deployment-acceptance"])
            env = campaign.child.call_args.kwargs["environment"]
            self.assertFalse(any(key.startswith(("DOCKER_", "COMPOSE_", "CONIX_MAKE_")) for key in env))

    def test_probe_allows_reviewed_sibling_imports_without_python_environment(self):
        """The isolated image owns /tests; interpreter isolation must allow its helpers."""
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.runner = "sha256:" + "a" * 64
            campaign.child = Mock(return_value=b'{}')
            campaign.probe("genesis")
            argv = campaign.child.call_args.args[0]
            image_index = argv.index(campaign.runner)
            self.assertEqual(argv[image_index + 1:], ["-E", "-s", "/tests/deployment_probe.py"])

    def test_consumer_cleanup_failure_preserves_dependencies_and_inputs(self):
        """SQL/RPC must remain inspectable if the retained consumer may still run."""
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.fixture_attempted = campaign.prepare_attempted = True
            campaign.make = Mock(side_effect=OperationError("Consumer retirement was not confirmed."))
            campaign.child = Mock()
            failures = campaign.cleanup()
            self.assertEqual(len(failures), 1)
            campaign.docker.remove_inputs.assert_not_called()
            campaign.child.assert_not_called()

    def test_reader_preparation_is_independent_and_uses_normal_make(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.child = Mock(return_value=b"")
            with patch.object(lane, "select_endpoint", return_value=campaign.docker.endpoint):
                for action in (
                    "deployment/reader/prepare",
                    "deployment/reader/check",
                    "deployment/reader/status",
                ):
                    campaign.make(action)
            actual = [call.args[0][2] for call in campaign.child.call_args_list]
            self.assertEqual(actual, [
                "deployment/reader/prepare",
                "deployment/reader/check",
                "deployment/reader/status",
            ])

    def test_unconfirmed_reader_retirement_preserves_external_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.fixture_attempted = campaign.prepare_attempted = True
            campaign.reader_prepare_attempted = True
            campaign.make = Mock(side_effect=[
                None, OperationError("Reader volume still mounted"),
            ])
            campaign.child = Mock()
            with (
                patch.object(lane, "_candidate_ids", return_value=[]),
                patch.object(lane.named, "inspect_reader_prepared",
                             return_value=SimpleNamespace(image="sha256:" + "a" * 64,
                                                          volume="conix-inputs-" + "b" * 24)),
            ):
                failures = campaign.cleanup()
            self.assertEqual(len(failures), 1)
            self.assertIn("Reader retirement", failures[0])
            campaign.docker.remove_inputs.assert_not_called()
            campaign.child.assert_not_called()

    def test_campaign_lock_refuses_overlapping_run(self):
        """The full acceptance test has one owner while child deployment commands lock separately."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "evidence"
            with lane.campaign_lock(folder):
                with self.assertRaises(OperationError):
                    with lane.campaign_lock(folder):
                        self.fail("An overlapping acceptance run acquired ownership.")

    def test_primary_failure_survives_unexpected_cleanup_failure(self):
        """Independent cleanup failure must be reported without hiding the failed operation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = OperationError("Fixture startup was refused.")
            campaign = Mock()
            campaign.phase = "fixture startup"
            campaign.records = {}
            campaign.execute.side_effect = original
            campaign.cleanup.side_effect = RuntimeError("untrusted cleanup detail")
            options = SimpleNamespace(deployment=lane.NAME, context="", wheelhouse="")
            decl = SimpleNamespace(docker_context="science", wheelhouse="/wheels")
            with (
                patch.object(lane.named, "ROOT", root),
                patch.object(lane, "fixture_permissions"),
                patch.object(lane.named, "inspect_named", return_value=decl),
                patch.object(lane, "select_endpoint", return_value="unix:///tmp/selected.sock"),
                patch.object(lane, "DeploymentAcceptance", return_value=campaign),
                patch("sys.stderr", new_callable=io.StringIO) as messages,
            ):
                with self.assertRaises(OperationError) as failure:
                    lane.run_acceptance(options, client_cls=Mock(), pinned_images=lambda: {})
            self.assertIs(failure.exception.__cause__, original)
            self.assertIn("Fixture startup", str(failure.exception))
            self.assertIn("Cleanup unconfirmed", messages.getvalue())
            self.assertNotIn("untrusted cleanup detail", messages.getvalue())

    def test_inspection_tracks_prepared_selection_without_private_lock_access(self):
        """Acceptance reads public resource facts and never parses TOML itself."""
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            prepared = SimpleNamespace(image="sha256:" + "a" * 64, volume="conix-inputs-" + "b" * 24)
            docker_record = {
                "Id": "a" * 64, "State": {"Running": True}, "Image": prepared.image,
                "Config": {"Cmd": ["-I", "-m", "conix", "run"]},
            }
            campaign.docker.run.return_value = json.dumps(docker_record)
            with (
                patch.object(lane, "_candidate_ids", return_value=["a" * 64]),
                patch.object(lane.named, "inspect_prepared", return_value=prepared) as inspect,
            ):
                self.assertEqual(campaign.running(), "a" * 64)
            inspect.assert_called_once_with(lane.NAME)

    def test_prepared_cleanup_requires_retired_volume_before_clearing(self):
        """A failed volume retirement must not discard its recovery lock."""
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.fixture_attempted = campaign.prepare_attempted = True
            campaign.make = Mock()
            campaign.child = Mock()
            prepared = SimpleNamespace(image="sha256:" + "a" * 64, volume="conix-inputs-" + "b" * 24)
            campaign.docker.remove_inputs.side_effect = OperationError("retirement unconfirmed")
            with (
                patch.object(lane.named, "inspect_prepared", return_value=prepared),
                patch.object(lane.named, "clear_prepared") as clear,
                patch.object(lane, "_candidate_ids", return_value=[]),
            ):
                failed = campaign.cleanup()
            clear.assert_not_called()
            self.assertTrue(any("staged inputs" in phase for phase in failed))

    def test_host_sigterm_uses_cleanup_path_and_restores_handler(self):
        """A terminating test controller must get the same cleanup opportunity as Ctrl-C."""
        import signal

        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):
            with cancellation():
                signal.raise_signal(signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_fixture_graph_refuses_host_access_and_image_drift(self):
        """Fixture provisioning must retain explicit images and internal-only reachability."""
        images = {"postgres": "postgres-pin", "foundry": "foundry-pin"}
        owner = "a" * 24
        graph = {"services": {}, "networks": {},
                 "volumes": {"pgdata": {"labels": {lane.OWNER_LABEL: owner}}}}
        for name in ("tests", "rpc"):
            graph["networks"][name] = {"internal": True, "labels": {lane.OWNER_LABEL: owner}}
        for name in ("postgres", "anvil", "bootstrap"):
            graph["services"][name] = {
                "image": images["foundry" if name == "anvil" else "postgres"],
                "user": "65532:65532" if name == "anvil" else "999:999",
                "pull_policy": "never", "read_only": True,
                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                "labels": {lane.OWNER_LABEL: owner},
            }
        lane.admit_fixture(graph, images, owner)
        for mutate in (
            lambda g: g["services"]["anvil"].update(image="other-image"),
            lambda g: g["services"]["postgres"].update(ports=["5432:5432"]),
            lambda g: g["services"]["postgres"].update(volumes=[{"type": "bind"}]),
            lambda g: g["networks"]["rpc"].update(internal=False),
            lambda g: g["volumes"]["pgdata"].update(driver_opts={"type": "none", "device": "/host"}),
        ):
            changed = copy.deepcopy(graph)
            mutate(changed)
            with self.assertRaises(OperationError):
                lane.admit_fixture(changed, images, owner)


if __name__ == "__main__":
    unittest.main()
