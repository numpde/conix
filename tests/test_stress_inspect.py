"""Independent raw WARC inspection, with no provider or Docker access."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from http_recording import RecordingTransport
import stress_inspect


SOURCE = json.loads((Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json").read_text())


class RawStressInspection(unittest.TestCase):
    def capture(self, folder, name, url, count):
        data = None

        def reply(request):
            nonlocal data
            request_id = json.loads(request.content)["id"]
            data = json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "number": "0x100", "hash": "0x" + "a" * 64,
                    "parentHash": "0x" + "b" * 64,
                },
            }, separators=(",", ":")).encode()
            return httpx.Response(200, content=data, headers={"Content-Type": "application/json"})

        with (folder / (name + ".warc.gz")).open("xb") as archive:
            recorder = RecordingTransport(
                httpx.MockTransport(reply), archive,
                max_request_bytes=16384, max_response_bytes=1024 * 1024,
                elapsed_budget_seconds=3, max_exchanges=8,
                max_archive_bytes=1024 * 1024,
            )
            with httpx.Client(transport=recorder, trust_env=False) as client:
                for number in range(1, count + 1):
                    response = client.post(url, json={
                        "jsonrpc": "2.0", "id": number,
                        "method": "eth_getBlockByNumber", "params": ["latest", False],
                    })
                    self.assertEqual(response.status_code, 200)
        return data

    def test_observer_archive_checks_request_response_metadata_and_raw_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            seed_reply = self.capture(folder, "observer", SOURCE["rpc_url"], 3)
            with patch.object(stress_inspect, "CAPTURE", folder):
                result = stress_inspect.inspect("observer")
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["exchanges"], 3)
            self.assertEqual(result["first_header"]["id"], 1)
            self.assertEqual(result["first_header"]["height"], 256)
            self.assertEqual(result["first_header"]["hash"], "0x" + "a" * 64)
            # All responses carry the same result except the RPC id, so the
            # first reply is compared against its exact independent body bytes.
            first_body = seed_reply.replace(b'"id":3,', b'"id":1,')
            self.assertEqual(result["first_header"]["body_sha256"], hashlib.sha256(first_body).hexdigest())

    def test_unreviewed_archive_target_is_rejected_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.capture(folder, "application", "https://example.com/other", 1)
            with patch.object(stress_inspect, "CAPTURE", folder):
                with self.assertRaisesRegex(ValueError, "another source"):
                    stress_inspect.inspect("application")


if __name__ == "__main__":
    unittest.main()
