"""Source-independent configuration, protocol and failure contracts for the RPC gateway."""

import json
import unittest
from unittest.mock import patch

import httpx

import rpc_gateway


class GatewayContracts(unittest.TestCase):
    def request(self, method="eth_getBlockByNumber", params=None):
        return {"jsonrpc": "2.0", "id": 1, "method": method, "params": ["latest", False] if params is None else params}

    def test_invalid_configuration_fails_before_client_or_listener_creation(self):
        """An invalid source grant must never begin network activity."""
        invalid = [(url, None) for url in (
            None, "", "http://rpc.example/", "https://rpc.example:8443/",
            "https://user:private@rpc.example/", "https://rpc.example/?key=private",
            "https://rpc.example/#fragment", "https://", "https://rpc.example/\n",
            "https://rpc.example:broken/",
        )]
        invalid += [("https://rpc.example/", ceiling) for ceiling in (True, -1, 1.5, "12")]
        for (upstream_url, ceiling) in invalid:
            with self.subTest(upstream_type=type(upstream_url).__name__, ceiling_type=type(ceiling).__name__):
                with patch.object(rpc_gateway.httpx, "Client") as client:
                    with patch.object(rpc_gateway, "ThreadingHTTPServer") as server:
                        with self.assertRaises(ValueError) as caught:
                            rpc_gateway.serve(upstream_url=upstream_url, head_ceiling=ceiling)
                        client.assert_not_called()
                        server.assert_not_called()
                        self.assertNotIn("private", str(caught.exception))

    def test_two_selected_upstreams_receive_only_the_original_read(self):
        """The scenario selects the endpoint; the RPC request cannot change it."""
        request = {"jsonrpc": "2.0", "id": 7, "method": "eth_chainId", "params": []}
        for url in ("https://first.example/rpc", "https://second.example:443/ext/bc/C/rpc"):
            seen = []

            def upstream(received):
                seen.append((received.url, json.loads(received.content)))
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": "0x1"})

            rpc_gateway.admit_configuration(url, None)
            with httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False) as client:
                result = rpc_gateway.forward(client, request, upstream_url=url)
            self.assertEqual(seen, [(httpx.URL(url), request)])
            self.assertEqual(result["result"], "0x1")

    def test_header_ceiling_is_configurable_and_does_not_rewrite_other_reads(self):
        """The ceiling projects header discovery, not an entire historical RPC view."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber", "params": ["latest", False]}
        for ceiling in (0, 12, 100):
            rpc_gateway.admit_configuration("https://rpc.example/", ceiling)
            selected, absent = rpc_gateway.bounded_view(request, head_ceiling=ceiling)
            self.assertEqual(selected["params"], [hex(ceiling), False])
            self.assertFalse(absent)
            beyond = {**request, "params": [hex(ceiling + 1), False]}
            self.assertTrue(rpc_gateway.bounded_view(beyond, head_ceiling=ceiling)[1])
            logs = {**request, "method": "eth_getLogs", "params": [{"toBlock": "latest"}]}
            self.assertEqual(rpc_gateway.bounded_view(logs, head_ceiling=ceiling), (logs, False))
        self.assertEqual(request["params"], ["latest", False])
        self.assertEqual(rpc_gateway.bounded_view(request, head_ceiling=None), (request, False))
        with self.assertRaises(rpc_gateway.GatewayRefusal):
            rpc_gateway.bounded_view({**request, "params": ["finalized", False]}, head_ceiling=12)

    def test_dns_control_resolves_the_configured_endpoint_on_the_admitted_port(self):
        """Direct-egress evidence must refer to the source actually selected."""
        records = [(rpc_gateway.socket.AF_INET, rpc_gateway.socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        for host in ("first.example", "second.example"):
            with patch.object(rpc_gateway.socket, "getaddrinfo", return_value=records) as resolve:
                self.assertEqual(rpc_gateway.upstream_addresses(upstream_url=f"https://{host}/rpc"), ["1.1.1.1"])
            resolve.assert_called_once_with(host, 443, type=rpc_gateway.socket.SOCK_STREAM)

    def test_writes_batches_unknown_methods_and_invalid_frames_are_refused(self):
        invalid = [self.request("eth_sendRawTransaction", []), self.request("eth_sendTransaction", []),
                   self.request("anvil_mine", []), self.request("debug_traceTransaction", []),
                   [self.request()], {**self.request(), "id": True}, {**self.request(), "method": {}},
                   {**self.request(), "upstream": "http://private/"}]
        for value in invalid:
            with self.subTest(kind=type(value).__name__), self.assertRaises(rpc_gateway.GatewayRefusal):
                rpc_gateway.admit(json.dumps(value).encode())
        for raw in (b"", b"not json"):
            with self.assertRaises(rpc_gateway.GatewayRefusal):
                rpc_gateway.admit(raw)

    def test_provider_prose_and_redirects_never_escape_the_gateway(self):
        secret = "postgresql://private:do-not-print@provider/db"
        with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"id": 1, "error": {"code": -32000, "message": secret}},
        )), trust_env=False) as client:
            result = rpc_gateway.forward(client, self.request(), upstream_url="https://rpc.example/")
        self.assertNotIn(secret, json.dumps(result))
        with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
            302, headers={"Location": "http://private/"},
        )), follow_redirects=False, trust_env=False) as client:
            with self.assertRaises(rpc_gateway.UpstreamFailure):
                rpc_gateway.forward(client, self.request(), upstream_url="https://rpc.example/")

    def test_response_size_limit_admits_the_boundary_and_refuses_one_more_byte(self):
        """Oversize rejection must precede parsing an otherwise valid response."""
        result = {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
        encoded = json.dumps(result).encode()
        payload = encoded + b" " * (rpc_gateway.MAX_RESPONSE_BYTES - len(encoded))
        for content in (payload, payload + b" "):
            with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
                200, content=content,
            )), trust_env=False) as client:
                if len(content) == len(payload):
                    self.assertEqual(rpc_gateway.forward(client, self.request(), upstream_url="https://rpc.example/"), result)
                else:
                    with self.assertRaises(rpc_gateway.UpstreamFailure) as caught:
                        rpc_gateway.forward(client, self.request(), upstream_url="https://rpc.example/")
                    self.assertEqual(caught.exception.cause, "reply_size")

    def test_provider_http_rejection_retains_its_status_as_upstream_failure(self):
        for status in (429, 503):
            with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
                status, text="private provider explanation",
            )), trust_env=False) as client:
                with self.assertRaises(rpc_gateway.UpstreamFailure) as caught:
                    rpc_gateway.forward(client, self.request(), upstream_url="https://rpc.example/")
            self.assertEqual(caught.exception.status, status)
            self.assertNotIn("private", str(caught.exception))

    def test_request_size_limit_admits_the_boundary_and_refuses_one_more_byte(self):
        """Count encoded input bytes before JSON decoding, including whitespace."""
        request = self.request("eth_chainId", [])
        encoded = json.dumps(request).encode()
        payload = encoded + b" " * (16 * 1024 - len(encoded))
        self.assertEqual(rpc_gateway.admit(payload), request)
        with self.assertRaises(rpc_gateway.GatewayRefusal):
            rpc_gateway.admit(payload + b" ")
