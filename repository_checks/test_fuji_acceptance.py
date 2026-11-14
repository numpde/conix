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
