"""Stage B grants exact retained inputs and keeps producer outcomes distinct."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import tarfile
import unittest
from unittest.mock import patch

from tools.containers.client import OperationError
from tools.fixtures.process import CampaignError
from tools.fixtures.stress_evidence import Completion, OWNER, bundle_inputs, run_completion
from tools.containers.diagnostics import PREFIX, admitted
from tools.fixtures.stress_capture import json_record
from tools.fixtures.stress_protocol import BUNDLE_INPUTS

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "sha256:" + "b" * 64
SELECTION = {"stress_completion_seconds": 60, "stress_exchanges": 4096, "stress_archive_bytes": 1048576}


class Docker:
    """Model immutable daemon identity; attached child streams are supplied separately."""

    docker_command_prefix = ["docker"]
    docker_environment = {}

    def __init__(self, root):
        self.working_dir = root
        self.present = True
        self.removed = False
        self.record = {
            "Id": "a" * 64, "Image": IMAGE, "Name": "/conix-evidence-complete-owned",
            "Config": {"Labels": {OWNER: "owned"}},
            "HostConfig": {"NetworkMode": "conix-evidence-owned", "LogConfig": {"Type": "none"}},
            "RestartCount": 0, "State": {"Running": False, "ExitCode": 0, "OOMKilled": False,
                                          "StartedAt": "2026-11-16T00:00:00Z", "FinishedAt": "2026-11-16T00:00:01Z"},
        }

    def run(self, *args, **kwargs):
        if args[0] == "ps":
            return self.record["Id"] if self.present else ""
        if args[:2] == ("container", "inspect"):
            return json.dumps(self.record)
        if args[:2] == ("container", "rm"):
            # Requiring retained evidence here proves ordering, not merely
            # whether the owner eventually called both operations.
            receipt = self.working_dir / "out/process-evidence" / (self.record["Id"] + ".json")
            if not receipt.exists():
                raise AssertionError("Removal preceded durable process evidence.")
            (self.removed, self.present) = True, False
            return ""
        raise AssertionError("Unrelated Docker operation was requested.")


class StressEvidenceContracts(unittest.TestCase):
    def test_bundle_selects_only_original_capture_files_and_projects_retained_catalog(self):
        """A nearby secret or current deployment change has no transport authority."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = {"rpc_url": "https://example.com/rpc"}
            files = {"source.json": json.dumps(source).encode(), "abi.json": b"[]",
                     "deployment.toml": (ROOT / "config/deployments/fuji-acceptance/deployment.toml").read_bytes(),
                     "application.warc.gz": b"application", "observer.warc.gz": b"observer"}
            hashes = {name: hashlib.sha256(data).hexdigest() for (name, data) in files.items()}
            manifest = {"capture": "complete", "source_sha256": hashes["source.json"],
                        "abi_sha256": hashes["abi.json"], "deployment_sha256": hashes["deployment.toml"],
                        "export": {name: {"sha256": hashes[name + ".warc.gz"]} for name in ("application", "observer")}}
            files["manifest.json"] = json.dumps(manifest).encode()
            for (name, data) in files.items():
                (folder / name).write_bytes(data)
                (folder / name).chmod(0o600)
            (folder / "secret.dsn").write_text("EXAMPLEONLY")
            (payload, selected) = bundle_inputs(folder)
            with tarfile.open(fileobj=io.BytesIO(payload)) as bundle:
                self.assertEqual(set(bundle.getnames()), BUNDLE_INPUTS.keys())
                catalog = bundle.extractfile("catalog.toml").read()
                self.assertIn(b"https://example.com/rpc", catalog)
                self.assertNotIn(b"EXAMPLEONLY", payload)
                self.assertEqual(selected["catalog.toml"], hashlib.sha256(catalog).hexdigest())

    def test_confined_worker_uses_private_streams_without_runtime_host_or_sql_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Completion(Docker(Path(directory)), IMAGE, Path(directory), "owned", SELECTION)
            for operation in ("admit", "complete"):
                args = owner.arguments(operation)
                self.assertIn("--interactive", args)
                self.assertNotIn("--tty", args)
                self.assertNotIn("--rm", args)
                self.assertNotIn("--volume", args)
                self.assertNotIn("--mount", args)
                self.assertEqual(args[args.index("--log-driver") + 1], "none")
                self.assertEqual(args[args.index("--network") + 1], "none" if operation == "admit" else owner.network)

    def test_failed_producer_is_removed_after_evidence_without_claiming_cleanup_failure(self):
        """Exit 7 remains primary even though its owned container was retired."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            docker = Docker(folder)
            docker.record["State"]["ExitCode"] = 7
            owner = Completion(docker, IMAGE, folder, "owned", SELECTION)

            def child(*args, output, error_file, **kwargs):
                output.write_bytes(b"partial WARC")
                error_file.write_bytes(b"")
                output.chmod(0o600)
                error_file.chmod(0o600)
                raise CampaignError("Producer failed.", exit_code=7)

            with patch("tools.fixtures.stress_evidence.owned_command", side_effect=child):
                with self.assertRaises(CampaignError) as caught:
                    owner.worker("complete", b"inputs")
            self.assertEqual(caught.exception.exit_code, 7)
            self.assertTrue(docker.removed)
            self.assertFalse(owner.records["cleanup"])
            self.assertEqual(owner.records["processes"]["complete"]["exit_code"], 7)

    def test_successful_attachment_without_stopped_container_evidence_is_refused(self):
        """An external removal cannot turn unavailable exit evidence into success."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            docker = Docker(folder)
            docker.present = False
            owner = Completion(docker, IMAGE, folder, "owned", SELECTION)
            with patch("tools.fixtures.stress_evidence.owned_command", return_value=None):
                with self.assertRaisesRegex(OperationError, "exit evidence"):
                    owner.worker("complete", b"inputs")

    def test_receipt_publication_failure_cannot_replace_the_original_producer_failure(self):
        """Disk failure after cleanup stays secondary to the admitted child error."""
        diagnostic = PREFIX + b'{"phase":"verification","cause":"RpcUnavailable","effect":"unconfirmed"}'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as bundle:
                member = tarfile.TarInfo("catalog.toml")
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
            options = SimpleNamespace(stress_completion_seconds="60", stress_exchanges="4096",
                                      stress_archive_bytes="1048576", capture_dir="retained", context="agentic", runner_image=IMAGE)
            docker = SimpleNamespace(require_rootless_engine=lambda: None, require_role=lambda *args: None)
            primary = CampaignError("Original producer failure.", exit_code=7, diagnostic=diagnostic)
            def fail_worker(*args):
                raise primary
            owner = SimpleNamespace(records={"status": "unavailable", "cleanup": {}},
                                    worker=fail_worker, cleanup=lambda: None)
            def publication(path, record):
                if path.name == "admission.json":
                    raise PermissionError("EXAMPLEONLY private publication path")
                json_record(path, record)
            with patch("tools.fixtures.stress_evidence.ROOT", root), \
                 patch("tools.fixtures.stress_evidence.bundle_inputs", return_value=(payload.getvalue(), {})), \
                 patch("tools.fixtures.stress_evidence.select_endpoint", return_value="selected"), \
                 patch("tools.fixtures.stress_evidence.Completion", return_value=owner), \
                 patch("tools.fixtures.stress_evidence.json_record", side_effect=publication), \
                 patch("tools.fixtures.stress_evidence.progress") as report:
                with self.assertRaises(OperationError) as caught:
                    run_completion(options, client_cls=lambda *args: docker)
            self.assertEqual(caught.exception.exit_code, 7)
            facts = admitted(caught.exception.diagnostic)
            self.assertEqual(facts["cause"], "RpcUnavailable")
            self.assertEqual(facts["child_exit"], 7)
            self.assertIn("RpcUnavailable", str(caught.exception))
            output = "\n".join(call.args[0] for call in report.call_args_list)
            self.assertIn("publication failed", output)
            self.assertNotIn("EXAMPLEONLY", output)


if __name__ == "__main__":
    unittest.main()
