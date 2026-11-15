"""Configuration and process contracts for the shared read-only RPC gateway."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import httpx

import fuji_rpc
import rpc_gateway


class GatewayConfigurationContracts(unittest.TestCase):
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

    def test_dns_control_resolves_the_configured_endpoint_on_the_admitted_port(self):
        """Direct-egress evidence must refer to the source actually selected."""
        records = [(rpc_gateway.socket.AF_INET, rpc_gateway.socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        for host in ("first.example", "second.example"):
            with patch.object(rpc_gateway.socket, "getaddrinfo", return_value=records) as resolve:
                self.assertEqual(rpc_gateway.upstream_addresses(upstream_url=f"https://{host}/rpc"), ["1.1.1.1"])
            resolve.assert_called_once_with(host, 443, type=rpc_gateway.socket.SOCK_STREAM)

    def test_fuji_launcher_derives_both_views_from_its_existing_source(self):
        """Fuji owns scenario values; the transport has no Fuji defaults."""
        source = json.loads((Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json").read_text())
        for (mode, ceiling) in (("live", None), ("historical", source["initial_interval"]["end_block"])):
            with patch.dict(os.environ, {"CONIX_FUJI_VIEW": mode}, clear=True):
                with patch.object(fuji_rpc, "serve") as serve:
                    fuji_rpc.main()
            serve.assert_called_once_with(upstream_url=source["rpc_url"], head_ceiling=ceiling)
        for mode in (None, "unknown"):
            environment = {} if mode is None else {"CONIX_FUJI_VIEW": mode}
            with patch.dict(os.environ, environment, clear=True), patch.object(fuji_rpc, "serve") as serve:
                with self.assertRaises(SystemExit):
                    fuji_rpc.main()
                serve.assert_not_called()

    def test_container_entrypoint_imports_the_shared_gateway_and_refuses_a_write(self):
        """The real launcher must work with its exact Python isolation flags."""
        launcher = Path(__file__).parent / "fuji_rpc.py"
        process = subprocess.Popen(
            [sys.executable, "-E", "-s", str(launcher)],
            env={"CONIX_FUJI_VIEW": "historical"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(trust_env=False, timeout=0.2) as client:
                deadline = time.monotonic() + 5
                while True:
                    self.assertIsNone(process.poll(), "Gateway launcher exited before becoming ready.")
                    try:
                        response = client.get("http://127.0.0.1:8080/healthz")
                        break
                    except httpx.RequestError:
                        self.assertLess(time.monotonic(), deadline, "Gateway launcher did not become ready.")
                        time.sleep(0.05)
                self.assertEqual(response.json(), {"view": "historical"})
                response = client.post("http://127.0.0.1:8080/", json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": ["private"],
                })
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["origin"], "admission")
                self.assertNotIn("private", response.text)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
