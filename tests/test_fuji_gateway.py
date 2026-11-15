"""Fuji source, launcher and confined-probe contracts for the live lane."""

import hashlib
import http.client
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
from conix.abi import compile_contract, decode_event, decode_return
from fuji_probe import position_record

SOURCE = json.loads((Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json").read_text())


class FujiGatewayContracts(unittest.TestCase):
    def test_built_image_contains_the_pinned_full_abi_and_decodes_real_creation(self):
        raw = (Path(__file__).parent / "fixtures/abi/fuji_pay_to_play.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), SOURCE["abi_sha256"])
        abi = json.loads(raw)
        signatures = tuple(e["name"] + "(" + ",".join(a["type"] for a in e["inputs"]) + ")"
                           for e in abi if e.get("type") == "event")
        contract = compile_contract(abi, signatures)
        self.assertEqual(len(contract.events), 17)
        names = []
        for log in SOURCE["expected_raw_logs"]:
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
        values = decode_return(function, bytes.fromhex(SOURCE["call_results"]["viewPosition"][2:]))
        fields = next(e for e in abi if e.get("name") == "viewPosition")["outputs"][0]["components"]
        self.assertEqual(position_record(fields, values[0]), SOURCE["position"])

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

        with patch.object(fuji_probe, "GATEWAY_URL", "http://selected-gateway:8080"):
            with patch.object(fuji_probe.urllib.request, "urlopen", return_value=Reply()) as open_url:
                with self.assertRaises(fuji_probe.ReadRejected) as caught:
                    fuji_probe.rpc("eth_getLogs", [])
        self.assertEqual(open_url.call_args.args[0].full_url, "http://selected-gateway:8080")
        self.assertEqual(caught.exception.code, -32602)
        self.assertEqual(caught.exception.method, "eth_getLogs")
        self.assertNotIn("private-provider-prose", str(caught.exception))

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

    def test_write_denial_probe_uses_the_same_selected_gateway(self):
        """The forbidden-write control must exercise the endpoint used for reads."""
        import io
        from types import SimpleNamespace
        import urllib.error
        import fuji_probe

        output = io.StringIO()
        request = SimpleNamespace(buffer=io.BytesIO(b'{"operation":"deny-write"}'))
        refused = urllib.error.HTTPError("http://selected-gateway:8080", 403, "private", {}, None)
        with patch.object(fuji_probe, "GATEWAY_URL", "http://selected-gateway:8080"):
            with patch.object(fuji_probe.urllib.request, "urlopen", side_effect=refused) as open_url:
                with patch.object(fuji_probe.sys, "stdin", request), patch.object(fuji_probe.sys, "stdout", output):
                    fuji_probe.main()
        self.assertEqual(open_url.call_args.args[0].full_url, "http://selected-gateway:8080")
        self.assertEqual(json.loads(output.getvalue()), {"transaction_method": "denied"})

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
            with patch.object(fuji_probe, "GATEWAY_URL", "http://selected-gateway:8080"):
                with patch.object(fuji_probe.urllib.request, "urlopen", return_value=response) as open_url:
                    with patch.object(fuji_probe.socket, "socket", side_effect=peers):
                        if outcomes[-1] is None:
                            with self.assertRaises(fuji_probe.CheckFailed) as caught:
                                fuji_probe.direct_upstream_denied()
                            self.assertEqual(caught.exception.check, "direct_egress")
                        else:
                            result = fuji_probe.direct_upstream_denied()
                            self.assertEqual(result["direct_upstream_tcp"], "blocked")
                            self.assertEqual(result["upstream_addresses"], addresses)
            self.assertEqual(open_url.call_args.args[0], "http://selected-gateway:8080/upstream-addresses")
            for peer, address in zip(peers, addresses):
                peer.connect.assert_called_once_with((address, 443))

    def test_probe_refuses_unsafe_address_evidence_before_opening_sockets(self):
        import io
        from unittest.mock import patch
        import fuji_probe
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

    def test_fuji_launcher_derives_both_views_from_its_existing_source(self):
        """Fuji owns scenario values; the transport has no Fuji defaults."""
        for (mode, ceiling) in (("live", None), ("historical", SOURCE["initial_interval"]["end_block"])):
            with patch.dict(os.environ, {"CONIX_FUJI_VIEW": mode}, clear=True):
                with patch.object(fuji_rpc, "serve") as serve:
                    fuji_rpc.main()
            serve.assert_called_once_with(upstream_url=SOURCE["rpc_url"], head_ceiling=ceiling)
        for mode in (None, "unknown"):
            environment = {} if mode is None else {"CONIX_FUJI_VIEW": mode}
            with patch.dict(os.environ, environment, clear=True), patch.object(fuji_rpc, "serve") as serve:
                with self.assertRaises(SystemExit):
                    fuji_rpc.main()
                serve.assert_not_called()

    def test_container_entrypoint_enforces_write_and_request_size_admission(self):
        """The real launcher must reject forbidden requests before contacting upstream."""
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
            # Send no body: reading before checking the declared size would stall.
            connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=1)
            try:
                connection.putrequest("POST", "/")
                connection.putheader("Content-Length", str(16 * 1024 + 1))
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertEqual(json.loads(response.read())["origin"], "admission")
            finally:
                connection.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
