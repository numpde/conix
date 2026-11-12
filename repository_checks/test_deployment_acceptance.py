"""Host contracts for the named deployment acceptance lane; no Docker required."""

import copy
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
            lock_file=folder / "deployment.lock.toml", docker_context="science",
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
            campaign.decl.lock_file.write_text("existing selection")
            with self.assertRaises(OperationError):
                campaign.preflight()
            campaign.docker.run.assert_not_called()
            self.assertEqual(campaign.decl.lock_file.read_text(), "existing selection")

    def test_existing_indexer_is_not_adopted_or_retired(self):
        """The stable deployment ID must not authorize touching an earlier service."""
        with tempfile.TemporaryDirectory() as directory:
            campaign = self.campaign(Path(directory))
            campaign.docker.run.return_value = "a" * 64
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
                patch.object(lane.named, "_read", return_value=decl),
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
