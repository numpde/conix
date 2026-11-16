"""Independent head-sample admission and private observer-control contracts."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import stress_control
from stress_observer import admit_window


def sample(instant, height=None, *, suffix="a"):
    return {
        "id": instant, "status": "unavailable" if height is None else "observed",
        "failure": "http_status" if height is None else None,
        "started_ns": instant, "response_ns": instant + 5,
        "finished_ns": instant + 10,
        "header": None if height is None else {
            "height": height, "hash": "0x" + suffix * 64, "parent_hash": "0x" + "b" * 64,
        },
    }


class ObservedWindowContracts(unittest.TestCase):
    def test_completed_short_gap_is_not_invented_head_evidence(self):
        points = [sample(0, 100), sample(1000), sample(2000, 102)]
        self.assertEqual(admit_window(points, 3000)["status"], "complete")
        self.assertEqual(admit_window(points, 1999)["reason"], "observation_gap")
        self.assertIsNone(points[1]["header"])

    def test_boundary_gap_reorg_decrease_and_empty_window_refuse_admission(self):
        cases = [
            [sample(0), sample(1000, 101)],
            [sample(0, 100), sample(1000)],
            [sample(0, 100), sample(1000, 100)],
            [sample(1000, 100), sample(0, 101)],
            [sample(0, 101), sample(1000, 100)],
            [sample(0, 100), sample(1000, 100, suffix="c"), sample(2000, 101)],
            [sample(0, 100)],
        ]
        for points in cases:
            with self.subTest(points=points):
                self.assertEqual(admit_window(points, 3000)["status"], "unavailable")

    def test_control_records_are_atomic_and_owner_private(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(stress_control, "CAPTURE", Path(folder)):
                stress_control.write_record("seed.json", {"height": 42})
                path = Path(folder) / "seed.json"
                self.assertEqual(json.loads(path.read_bytes()), {"height": 42})
                self.assertEqual(path.stat().st_mode & 0o077, 0)
                self.assertFalse((Path(folder) / "seed.json.new").exists())


if __name__ == "__main__":
    unittest.main()
