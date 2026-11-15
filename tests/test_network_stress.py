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
                if result["model"] == "resource_ceiling":
                    self.assertEqual(sink.publications, [])
                    self.assertIsNone(result["capacity_upper_bound_blocks_per_second"])
                    continue
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
                elif result["model"] == "rpc_at_capacity":
                    self.assertEqual(result["capacity_upper_bound_blocks_per_second"], result["demand_blocks_per_second"])
                else:
                    self.assertIsNone(result["capacity_upper_bound_blocks_per_second"])
                self.assertGreater(result["local_scanner_seconds"], 0)

    async def test_observed_current_and_historical_rates_drive_quiet_contract_stress(self):
        from collections import Counter
        import math
        from network_stress import measured_workloads
        folder = Path(__file__).parent / "fixtures/evm"
        observations = json.loads((folder / "network_rates.json").read_text())
        specification = json.loads((folder / "network_stress_workloads.json").read_text())
        sources = json.loads((folder / "network_rate_sources.json").read_text())
        outcomes = Counter()
        covered = set()
        current = []
        for context, workload in measured_workloads(observations, specification):
            covered.add(context["network"])
            if workload is None:
                outcomes[context["basis"]] += 1
                continue
            with self.subTest(network=context["network"], window=context["window"], basis=context["basis"]):
                result, sink, source = await replay(workload)
                outcomes[result["model"]] += 1
                if result["model"] == "resource_ceiling":
                    self.assertEqual(sink.publications, [])
                    self.assertIsNone(result["capacity_upper_bound_blocks_per_second"])
                    continue
                self.assertEqual(result["completed_blocks"], workload["blocks"])
                self.assertEqual([block.number for block, _ in sink.publications], list(range(1, workload["blocks"] + 1)))
                for block, events in sink.publications:
                    self.assertEqual(events, ())
                    for topic in source.selectors:
                        self.assertEqual(source.requests[(source.block_hash(block.number), topic)], 1)
                expected_cost = workload["blocks"] * result["modeled_steady_rpc_ms_per_block"] - workload["header_ms"]
                self.assertTrue(math.isclose(result["modeled_elapsed_ms"], expected_cost, abs_tol=1e-8))
                if result["model"] in ("rpc_deadline_exceeded", "rpc_cost_unresolved"):
                    self.assertIsNone(result["capacity_upper_bound_blocks_per_second"])
                if context["window"] == "head" and context["basis"] == "observed_reference_selector_cost":
                    current.append({**context, **result})
        self.assertEqual(covered, {network["id"] for network in sources["networks"]})
        self.assertGreater(outcomes["rpc_bottleneck"], 0)
        self.assertGreater(outcomes["rpc_headroom"], 0)
        print("Network-rate stand-in outcomes: " + json.dumps(dict(outcomes), sort_keys=True), flush=True)
        print("Current reference-cost model (no SQL/scheduling): " + json.dumps(current, sort_keys=True), flush=True)


    async def test_paced_relay_retires_accepted_callbacks_before_its_client(self):
        import asyncio
        from unittest.mock import AsyncMock, Mock
        from test_t0_7e_faults import PacedRelay
        relay = PacedRelay("http://unused.invalid", initial=1, ceiling=10,
                           interval_seconds=0.2, latency_seconds=0.001)
        relay.server = Mock()
        relay.server.wait_closed = AsyncMock()
        relay.client = Mock()
        relay.client.aclose = AsyncMock()
        relay.client.post = AsyncMock(side_effect=AssertionError("A retired client must never receive a request."))
        reader = asyncio.StreamReader()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber", "params": ["0x1", False]}).encode()
        reader.feed_data(b"POST / HTTP/1.1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        writer = Mock()
        writer.wait_closed = AsyncMock()
        accepted = asyncio.create_task(relay.handle(reader, writer))
        await asyncio.wait_for(relay.acquiring.wait(), timeout=1)
        await relay.__aexit__(None, None, None)
        self.assertTrue(accepted.done())
        self.assertTrue(accepted.cancelled())
        self.assertEqual(relay.handlers, set())
        relay.client.post.assert_not_called()
        relay.client.aclose.assert_awaited_once()
        writer.close.assert_called_once()
