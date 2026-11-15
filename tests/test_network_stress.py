"""Stand-in acquisition preserves empty-block coverage and complete raw events."""

import json
from pathlib import Path
import unittest

from network_stress import replay


class NetworkStressContracts(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_workloads_use_original_acquisition_and_match_capacity_model(self):
        cases = json.loads((Path(__file__).parent / "fixtures/evm/network_stress_workloads.json").read_text())["cases"]
        for workload in cases:
            with self.subTest(workload=workload["id"]):
                result, sink, source = await replay(workload)
                self.assertEqual(result["model"], workload["expected_model"])
                self.assertEqual([block.number for block, _ in sink.publications], list(range(1, workload["blocks"] + 1)))
                expected_events = workload["blocks"] // workload["emit_every_n_blocks"] * workload["events_per_emitting_block"]
                self.assertEqual(sum(len(events) for _, events in sink.publications), expected_events)
                for block, _events in sink.publications:
                    for topic in source.selectors:
                        self.assertEqual(source.requests[(source.block_hash(block.number), topic)], 1)
                    count = workload["events_per_emitting_block"] if block.number % workload["emit_every_n_blocks"] == 0 else 0
                    self.assertEqual(len(_events), count)
                    self.assertEqual([event.log_index for event in _events], list(range(count)))
                    for event in _events:
                        self.assertEqual(event.transaction_index, 0)
                        self.assertIn("0x" + event.transaction_hash.hex(), source.headers[source.block_hash(block.number)]["transactions"])
                expected_cost = workload["blocks"] * result["modeled_steady_rpc_ms_per_block"] - workload["header_ms"]
                self.assertEqual(result["modeled_elapsed_ms"], expected_cost)
                if result["model"] == "rpc_bottleneck":
                    self.assertGreater(result["final_backlog"], result["initial_backlog"])
                elif result["model"] == "rpc_headroom":
                    self.assertLess(result["final_backlog"], result["initial_backlog"])
                else:
                    self.assertIsNone(result["capacity_upper_bound_blocks_per_second"])
                self.assertGreater(result["local_scanner_seconds"], 0)
