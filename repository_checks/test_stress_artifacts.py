"""Dated stress evidence stays discoverable without publishing private output."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.containers.client import OperationError
from tools.fixtures.deployment import campaign_lock
from tools.fixtures.stress_artifacts import PRIVATE_RUNS, PUBLIC_RUNS, allocate_run, publish_report, source_subject

WHEN = datetime(2026, 11, 16, 15, 54, 40, tzinfo=timezone.utc)


class StressArtifactContracts(unittest.TestCase):
    def evidence(self, root):
        folder = allocate_run(root, "fuji-pay-to-play", "evidence-completion", when=WHEN)
        archive = b"EXAMPLEONLY raw provider credential"
        (folder / "completion.warc.gz").write_bytes(archive)
        records = {"status": "complete", "owner": "ce496dc4bca1c8327da4f6e4", "cleanup": {}, "completion_sha256": hashlib.sha256(archive).hexdigest(),
                   "engine": "EXAMPLEONLY private socket", "argv": ["EXAMPLEONLY"],
                   "stderr": "EXAMPLEONLY private failure", "source_url": "https://EXAMPLEONLY.invalid"}
        (folder / "admission.json").write_text(json.dumps(records))
        return folder, records

    def test_dated_allocation_is_exclusive_and_keeps_the_first_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with campaign_lock(root / PRIVATE_RUNS):
                folder = allocate_run(root, "fuji-pay-to-play", "capture", when=WHEN)
                marker = folder / "manifest.json"
                marker.write_text("retained")
                with self.assertRaises(FileExistsError):
                    allocate_run(root, "fuji-pay-to-play", "capture", when=WHEN)
                self.assertEqual(marker.read_text(), "retained")
                self.assertEqual(folder.relative_to(root / PRIVATE_RUNS).as_posix(), "20261116-155440/fuji-pay-to-play/capture")

    def test_invalid_subject_never_allocates_an_output_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with campaign_lock(root / PRIVATE_RUNS):
                for name in ("../elsewhere", "foo/bar", "", "Fuji", "x" * 65):
                    with self.subTest(name=name), self.assertRaises(OperationError):
                        allocate_run(root, name, "capture", when=WHEN)
                self.assertFalse(any((root / PRIVATE_RUNS).iterdir()))

    def test_public_report_projects_hashes_without_recursive_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with campaign_lock(root / PRIVATE_RUNS):
                (folder, records) = self.evidence(root)
                report = publish_report(root, folder, records, original_directory="out/rpc-evidence/ce496dc4bca1c8327da4f6e4")
                serialized = report.read_text()
                self.assertNotIn("EXAMPLEONLY", serialized)
                facts = json.loads(serialized)
                self.assertEqual(facts["archives"]["completion.warc.gz"]["sha256"], records["completion_sha256"])
                self.assertEqual(facts["receipt"]["sha256"], hashlib.sha256((folder / "admission.json").read_bytes()).hexdigest())
                self.assertEqual(report.relative_to(root / PUBLIC_RUNS).as_posix(), "20261116-155440/fuji-pay-to-play/evidence-completion/report.json")
                self.assertEqual(facts["original_private_directory"], "out/rpc-evidence/ce496dc4bca1c8327da4f6e4")

    def test_report_publication_never_replaces_prior_review_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with campaign_lock(root / PRIVATE_RUNS):
                (folder, records) = self.evidence(root)
                report = publish_report(root, folder, records)
                original = report.read_bytes()
                with self.assertRaises(FileExistsError):
                    publish_report(root, folder, records)
                self.assertEqual(report.read_bytes(), original)

    def test_unverified_partial_archive_is_recorded_without_claiming_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with campaign_lock(root / PRIVATE_RUNS):
                (folder, records) = self.evidence(root)
                records["status"] = "unavailable"
                del records["completion_sha256"]
                (folder / "admission.json").write_text(json.dumps(records))
                report = publish_report(root, folder, records)
                facts = json.loads(report.read_text())
                self.assertEqual(facts["status"], "unavailable")
                self.assertEqual(facts["archives"]["completion.warc.gz"]["verification"], "unconfirmed")

    def test_empty_failed_stream_still_produces_a_reviewable_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with campaign_lock(root / PRIVATE_RUNS):
                (folder, records) = self.evidence(root)
                records["status"] = "unavailable"
                del records["completion_sha256"]
                (folder / "completion.warc.gz").write_bytes(b"")
                (folder / "admission.json").write_text(json.dumps(records))
                report = publish_report(root, folder, records)
                archive = json.loads(report.read_text())["archives"]["completion.warc.gz"]
                self.assertEqual(archive["bytes"], 0)
                self.assertEqual(archive["verification"], "unconfirmed")
                records["status"] = "complete"
                with self.assertRaises(OperationError):
                    publish_report(root, folder, records)

    def test_subject_uses_public_source_identity_and_never_a_private_directory_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "fuji_pay_to_play.json"
            source = {"chain_id": 43113, "genesis_hash": "0x" + "a" * 64, "address": "0x" + "b" * 40}
            reviewed.write_text(json.dumps(source))
            source["private_directory"] = "out/customer-EXAMPLEONLY-key/capture"
            self.assertEqual(source_subject(source, reviewed), "fuji-pay-to-play")
            source["address"] = "0x" + "c" * 40
            self.assertEqual(source_subject(source, reviewed), "chain-43113")
            with campaign_lock(root / PRIVATE_RUNS):
                (folder, records) = self.evidence(root)
                with self.assertRaises(OperationError):
                    publish_report(root, folder, records, original_directory="out/customer-EXAMPLEONLY-key/capture")


if __name__ == "__main__":
    unittest.main()
