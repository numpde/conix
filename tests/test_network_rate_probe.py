"""Measure actual numbered-block spans without manufacturing missing evidence."""

from copy import deepcopy
import json
import unittest

import httpx

from network_rate_probe import sample_network
from test_core_rpc import json_response


class NetworkRateProbeContracts(unittest.TestCase):
    def setUp(self):
        self.network = {"id": "sample", "chain_id": 31337, "rpc_url": "https://selected.invalid", "retired": False}
        self.source = {"sampling": {"request_timeout_seconds": 1, "network_budget_seconds": 5, "response_limit_bytes": 1048576,
                                    "height_positions_percent": [100, 50], "window_span_blocks": 8, "contiguous_tail_blocks": 3},
                       "selector_probe": {"address": "0x" + "00" * 20, "topic0": "0x" + "11" * 32}}
        self.mutate = None
        self.seen = []

    def respond(self, request):
        operation = json.loads(request.content)
        self.seen.append(operation)
        method, arguments = operation["method"], operation["params"]
        if method == "eth_chainId":
            result = "0x7a69"
        elif method == "eth_getLogs":
            result = []
        else:
            number = 10000 if arguments[0] == "latest" else int(arguments[0], 16)
            result = {"number": hex(number), "hash": "0x" + f"{number:064x}", "parentHash": "0x" + f"{max(0,number-1):064x}",
                      "timestamp": hex(1700000000 + number // 5), "timestampMs": hex(1700000000000 + number * 200), "transactions": []}
        document = {"jsonrpc": "2.0", "id": operation["id"], "result": result}
        if self.mutate:
            document = self.mutate(operation, document)
        return json_response(document)

    def sample(self):
        return sample_network(self.network, self.source, transport=httpx.MockTransport(self.respond))

    def test_subsecond_cadence_is_not_rounded_to_a_seconds_timestamp(self):
        observed = self.sample()
        self.assertEqual(observed["status"], "observed")
        for window in observed["windows"]:
            self.assertEqual(window["mean_numbered_block_interval_ms"], 200)
            self.assertEqual(window["timestamp_resolution_ms"], 1)
            self.assertEqual([item["height"] for item in window["tail"]], list(range(window["last"]["height"] - 2, window["last"]["height"] + 1)))
            self.assertEqual(window["selector_probe"]["status"], "observed")
            self.assertEqual(window["selector_probe"]["log_count"], 0)

    def test_missing_headers_and_provider_rejections_are_not_zero_traffic(self):
        for cause in ("missing_header", "rpc_rejection"):
            def mutate(operation, document):
                if operation["method"] == "eth_getBlockByNumber":
                    return {**document, "result": None} if cause == "missing_header" else {
                        "jsonrpc": "2.0", "id": operation["id"], "error": {"code": -32005, "message": "private-provider-prose"},
                    }
                return document
            self.mutate = mutate
            result = self.sample()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["failure"]["cause"], cause)
            self.assertNotIn("private-provider-prose", json.dumps(result))

    def test_a_changed_anchor_invalidates_the_rate_window(self):
        reads = 0
        def mutate(operation, document):
            nonlocal reads
            if operation["method"] == "eth_getBlockByNumber" and operation["params"][0] == hex(10000):
                reads += 1
                if reads == 2:
                    document["result"]["hash"] = "0x" + "ab" * 32
            return document
        self.mutate = mutate
        result = self.sample()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["windows"][0]["failure"]["cause"], "changed_window_anchor")

    def test_unavailable_genesis_does_not_invent_or_discard_current_rate_evidence(self):
        def mutate(operation, document):
            if operation["method"] == "eth_getBlockByNumber" and operation["params"][0] == "0x0":
                return {"jsonrpc": "2.0", "id": operation["id"], "error": {"code": 4444, "message": "private-provider-prose"}}
            return document
        self.mutate = mutate
        result = self.sample()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["genesis"]["status"], "unavailable")
        self.assertEqual(result["identity_assurance"], "chain_id_checked")
        self.assertEqual(result["windows"][0]["mean_numbered_block_interval_ms"], 200)
        self.assertNotIn("private-provider-prose", json.dumps(result))

    def test_retirement_never_contacts_a_provider(self):
        self.network.update(retired=True, rpc_url=None)
        self.assertEqual(self.sample()["status"], "retired")
        self.assertEqual(self.seen, [])
