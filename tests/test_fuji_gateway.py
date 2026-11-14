"""Offline proof of the exact gateway and deployed ABI used by the live lane."""

import hashlib
import json
from pathlib import Path
import unittest

import httpx

import fuji_rpc
from conix.abi import compile_contract, decode_event, decode_return
from fuji_probe import position_record


class FujiGatewayContracts(unittest.TestCase):
    def request(self, method="eth_getBlockByNumber", params=None):
        return {"jsonrpc": "2.0", "id": 1, "method": method, "params": ["latest", False] if params is None else params}

    def test_read_is_forwarded_to_only_the_fixed_https_upstream(self):
        seen = []

        def upstream(request):
            seen.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0xa869"})

        request = self.request("eth_chainId", [])
        with httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False, follow_redirects=False) as client:
            result = fuji_rpc.forward(client, fuji_rpc.admit(json.dumps(request).encode()))
        self.assertEqual(seen, [(fuji_rpc.SOURCE["rpc_url"], request)])
        self.assertEqual(result["result"], "0xa869")

    def test_writes_batches_unknown_methods_and_invalid_frames_are_refused(self):
        invalid = [self.request("eth_sendRawTransaction", []), self.request("eth_sendTransaction", []),
                   self.request("anvil_mine", []), self.request("debug_traceTransaction", []),
                   [self.request()], {**self.request(), "id": True}, {**self.request(), "method": {}},
                   {**self.request(), "upstream": "http://private/"}]
        for value in invalid:
            with self.subTest(kind=type(value).__name__), self.assertRaises(fuji_rpc.GatewayRefusal):
                fuji_rpc.admit(json.dumps(value).encode())
        for raw in (b"", b"not json", b" " * 16385):
            with self.assertRaises(fuji_rpc.GatewayRefusal):
                fuji_rpc.admit(raw)

    def test_historical_head_is_explicit_and_live_head_is_unmodified(self):
        request = self.request()
        selected, absent = fuji_rpc.bounded_view(request, True)
        self.assertFalse(absent)
        self.assertEqual(selected["params"], [hex(fuji_rpc.SOURCE["initial_interval"]["end_block"]), False])
        self.assertEqual(fuji_rpc.bounded_view(request, False), (request, False))
        beyond = self.request(params=[hex(fuji_rpc.SOURCE["initial_interval"]["end_block"] + 1), False])
        self.assertTrue(fuji_rpc.bounded_view(beyond, True)[1])
        with self.assertRaises(fuji_rpc.GatewayRefusal):
            fuji_rpc.bounded_view(self.request(params=["finalized", False]), True)

    def test_provider_prose_and_redirects_never_escape_the_gateway(self):
        secret = "postgresql://private:do-not-print@provider/db"
        with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"id": 1, "error": {"code": -32000, "message": secret}},
        )), trust_env=False) as client:
            result = fuji_rpc.forward(client, self.request())
        self.assertNotIn(secret, json.dumps(result))
        with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
            302, headers={"Location": "http://private/"},
        )), follow_redirects=False, trust_env=False) as client:
            with self.assertRaises(fuji_rpc.UpstreamFailure):
                fuji_rpc.forward(client, self.request())

    def test_oversized_reply_is_refused(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, content=b" " * (fuji_rpc.MAX_BYTES + 1),
        )), trust_env=False) as client:
            with self.assertRaises(fuji_rpc.UpstreamFailure):
                fuji_rpc.forward(client, self.request())

    def test_built_image_contains_the_pinned_full_abi_and_decodes_real_creation(self):
        raw = (Path(__file__).parent / "fixtures/abi/fuji_pay_to_play.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), fuji_rpc.SOURCE["abi_sha256"])
        abi = json.loads(raw)
        signatures = tuple(e["name"] + "(" + ",".join(a["type"] for a in e["inputs"]) + ")"
                           for e in abi if e.get("type") == "event")
        contract = compile_contract(abi, signatures)
        self.assertEqual(len(contract.events), 17)
        names = []
        for log in fuji_rpc.SOURCE["expected_raw_logs"]:
            topics = tuple(bytes.fromhex(value[2:]) for value in log["topics"])
            descriptor = next(event for event in contract.events if event.topic0 == topics[0])
            decoded = decode_event(descriptor, topics, bytes.fromhex(log["data"][2:]))
            self.assertTrue(decoded)
            names.append(descriptor.signature)
        self.assertEqual(set(names), {"OwnershipTransferred(address,address)", "ChannelCreated(uint256,address,uint64)"})

    def test_actual_historical_position_uses_the_original_reader_return_shape(self):
        raw = (Path(__file__).parent / "fixtures/abi/fuji_pay_to_play.json").read_bytes()
        abi = json.loads(raw)
        signatures = tuple(e["name"] + "(" + ",".join(a["type"] for a in e["inputs"]) + ")"
                           for e in abi if e.get("type") == "event")
        compiled = compile_contract(abi, signatures)
        function = next(f for f in compiled.functions if f.signature == "viewPosition(address,uint256,address)")
        values = decode_return(function, bytes.fromhex(fuji_rpc.SOURCE["call_results"]["viewPosition"][2:]))
        fields = next(e for e in abi if e.get("name") == "viewPosition")["outputs"][0]["components"]
        self.assertEqual(position_record(fields, values[0]), fuji_rpc.SOURCE["position"])

    def test_probe_keeps_numeric_provider_category_without_private_prose(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import fuji_probe

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"error": {"code": -32602, "message": "private-provider-prose"}}).encode()

        with patch.object(fuji_probe.urllib.request, "urlopen", return_value=Reply()):
            with self.assertRaises(fuji_probe.ReadRejected) as caught:
                fuji_probe.rpc("eth_getLogs", [])
        self.assertEqual(caught.exception.code, -32602)
        self.assertEqual(caught.exception.method, "eth_getLogs")
        self.assertNotIn("private-provider-prose", str(caught.exception))


    def test_provider_http_rejection_retains_its_status_as_upstream_failure(self):
        for status in (429, 503):
            with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
                status, text="private provider explanation",
            )), trust_env=False) as client:
                with self.assertRaises(fuji_rpc.UpstreamFailure) as caught:
                    fuji_rpc.forward(client, self.request())
            self.assertEqual(caught.exception.status, status)
            self.assertNotIn("private", str(caught.exception))


    def test_probe_preserves_upstream_status_but_rejects_untrusted_prose(self):
        import io
        import urllib.error
        from unittest.mock import patch
        import fuji_probe
        body = json.dumps({"origin": "upstream", "cause": "http_status", "status": 429,
                           "error": "private-provider-prose"}).encode()
        error = urllib.error.HTTPError("http://gateway", 502, "private", {}, io.BytesIO(body))
        with patch.object(fuji_probe.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(fuji_probe.ReadRejected) as caught:
                fuji_probe.rpc("eth_getLogs", [])
        self.assertEqual(caught.exception.upstream, "http_status status 429")
        self.assertEqual(caught.exception.code, 502)
        self.assertNotIn("private", str(caught.exception))

    def test_known_source_failure_exposes_its_check_without_rpc_values(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import patch
        import fuji_probe
        with patch.object(fuji_probe.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"operation":"source"}'))):
            with patch.object(fuji_probe, "rpc", return_value="0x1"):
                with self.assertRaises(SystemExit) as caught:
                    fuji_probe.main()
        self.assertIn("Check chain.", str(caught.exception))
        self.assertIn("CheckFailed", str(caught.exception))
        self.assertNotIn("0x1", str(caught.exception))



    def test_missing_gateway_dns_does_not_prove_direct_upstream_denial(self):
        import urllib.error
        from unittest.mock import patch
        import fuji_probe
        with patch.object(fuji_probe.urllib.request, "urlopen", side_effect=urllib.error.URLError("private")):
            with self.assertRaises(fuji_probe.CheckFailed) as caught:
                fuji_probe.direct_upstream_denied()
        self.assertEqual(caught.exception.check, "upstream_dns")

    def test_direct_denial_requires_every_resolved_destination_to_refuse(self):
        import io
        from unittest.mock import MagicMock, patch
        import fuji_probe
        addresses = ["1.1.1.1", "8.8.8.8"]
        for outcomes in ((OSError("unreachable"), OSError("unreachable")), (OSError("unreachable"), None)):
            peers = [MagicMock() for _ in outcomes]
            for peer, outcome in zip(peers, outcomes):
                peer.__enter__.return_value = peer
                peer.connect.side_effect = outcome
            response = io.BytesIO(json.dumps({"addresses": addresses}).encode())
            with patch.object(fuji_probe.urllib.request, "urlopen", return_value=response):
                with patch.object(fuji_probe.socket, "socket", side_effect=peers):
                    if outcomes[-1] is None:
                        with self.assertRaises(fuji_probe.CheckFailed) as caught:
                            fuji_probe.direct_upstream_denied()
                        self.assertEqual(caught.exception.check, "direct_egress")
                    else:
                        result = fuji_probe.direct_upstream_denied()
                        self.assertEqual(result["direct_upstream_tcp"], "blocked")
                        self.assertEqual(result["upstream_addresses"], addresses)
            for peer, address in zip(peers, addresses):
                peer.connect.assert_called_once_with((address, 443))

    def test_address_evidence_is_fixed_to_public_upstream_and_admitted_before_use(self):
        import io
        import socket
        from unittest.mock import patch
        import fuji_probe
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        with patch.object(fuji_rpc.socket, "getaddrinfo", return_value=records) as resolve:
            self.assertEqual(fuji_rpc.upstream_addresses(), ["1.1.1.1"])
        self.assertEqual(resolve.call_args.args[1], 443)
        self.assertIn(resolve.call_args.args[0], fuji_rpc.SOURCE["rpc_url"])
        for addresses in ([], ["127.0.0.1"], ["private"], [1], ["1.1.1.1"] * 9):
            with patch.object(fuji_probe.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"addresses": addresses}).encode())):
                with patch.object(fuji_probe.socket, "socket") as connect:
                    with self.assertRaises(fuji_probe.CheckFailed):
                        fuji_probe.direct_upstream_denied()
                    connect.assert_not_called()


    def test_unreadable_gateway_error_body_keeps_the_http_failure(self):
        import urllib.error
        from unittest.mock import patch
        import fuji_probe
        error = urllib.error.HTTPError("http://gateway", 502, "private", {}, None)
        with patch.object(error, "read", side_effect=OSError("private")):
            with patch.object(fuji_probe.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(fuji_probe.ReadRejected) as caught:
                    fuji_probe.rpc("eth_getLogs", [])
        self.assertEqual(caught.exception.code, 502)
        self.assertIsNone(caught.exception.upstream)
