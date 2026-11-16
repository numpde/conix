"""Real WARC serialization contracts for reusable stress evidence admission."""

import gzip
import json
import re
from pathlib import Path
import tempfile
import unittest

import httpx

from http_recording import RecordingTransport
from stress_archive import read_archive, rpc_key

URL = "https://example.com/rpc"


class StressArchiveContracts(unittest.TestCase):
    def capture(self, path, *, body=None, status=200, encoding=None, count=1, protocol=b"HTTP/1.1"):
        """Preserve real recorder bytes while varying only provider behavior."""
        def reply(request):
            identifier = json.loads(request.content)["id"]
            payload = body if body is not None else json.dumps({
                "jsonrpc": "2.0", "id": identifier, "result": [],
            }).encode()
            headers = {} if encoding is None else {"Content-Encoding": encoding}
            return httpx.Response(status, content=payload, headers=headers, extensions={"http_version": protocol})

        with path.open("xb") as archive:
            transport = RecordingTransport(
                httpx.MockTransport(reply), archive, max_request_bytes=16384,
                max_response_bytes=2 * 1024 * 1024, elapsed_budget_seconds=10,
                max_exchanges=8, max_archive_bytes=4 * 1024 * 1024,
            )
            with httpx.Client(transport=transport, trust_env=False) as client:
                for identifier in range(1, count + 1):
                    client.post(URL, json={"jsonrpc": "2.0", "id": identifier,
                                           "method": "eth_getLogs", "params": [{}]})

    def test_empty_results_and_repeated_requests_keep_their_own_identity(self):
        """Repeated immutable reads remain ordered and independently correlated."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.warc.gz"
            self.capture(path, count=2)
            exchanges = read_archive(path, URL)
            self.assertEqual([exchange.request["id"] for exchange in exchanges], [1, 2])
            self.assertEqual([exchange.result() for exchange in exchanges], [[], []])
            self.assertEqual(exchanges[0].key, exchanges[1].key)
            self.assertNotEqual(rpc_key("call", [True]), rpc_key("call", [1]))

    def test_gzip_response_is_decoded_without_changing_archive(self):
        """Encoded provider bytes remain portable while the lookup uses JSON."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.warc.gz"
            payload = b'{"jsonrpc":"2.0","id":1,"result":[]}'
            self.capture(path, body=gzip.compress(payload), encoding="gzip")
            original = path.read_bytes()
            self.assertEqual(read_archive(path, URL)[0].result(), [])
            self.assertEqual(path.read_bytes(), original)

    def test_complete_provider_failure_is_retained_but_cannot_fill_matrix(self):
        """A recorded unsuccessful poll is evidence, never an empty result."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.warc.gz"
            self.capture(path, body=b"EXAMPLEONLY private provider prose", status=429, encoding="unsupported")
            exchange = read_archive(path, URL)[0]
            self.assertEqual(exchange.status, 429)
            with self.assertRaisesRegex(ValueError, "non-200"):
                exchange.result()

    def test_ambiguous_json_and_boolean_or_mismatched_reply_ids_are_refused(self):
        """Successful HTTP cannot turn an unrelated RPC result into evidence."""
        for body in (
            b'{"jsonrpc":"2.0","id":true,"result":[]}',
            b'{"jsonrpc":"2.0","id":2,"result":[]}',
            b'{"jsonrpc":"2.0","id":1,"result":[],"result":null}',
            b'{"jsonrpc":"2.0","id":1,"result":NaN}',
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "recording.warc.gz"
                self.capture(path, body=body)
                with self.assertRaises(ValueError):
                    read_archive(path, URL)[0].result()

    def test_altered_digest_and_unfinished_conversation_are_refused(self):
        """Neither readable corruption nor missing message metadata is finalized."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.warc.gz"
            self.capture(path)
            original = gzip.decompress(path.read_bytes())
            path.write_bytes(original.replace(b'"result": []', b'"result": {}'))
            with self.assertRaisesRegex(ValueError, "digest"):
                read_archive(path, URL)
            # Remove a whole final metadata record, leaving valid individual
            # record digests but an incomplete conversation.
            final_record = original.rfind(b"WARC/1.1\r\n")
            path.write_bytes(original[:final_record])
            with self.assertRaisesRegex(ValueError, "incomplete"):
                read_archive(path, URL)

    def test_payload_digest_alone_cannot_admit_unverified_http_envelopes(self):
        """Evidence must cover HTTP headers as well as their JSON payload."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.warc.gz"
            self.capture(path)
            raw = gzip.decompress(path.read_bytes())
            # Leave warcinfo intact; omit the block digest of the request.
            marker = raw.index(b"WARC-Type: request")
            prefix, request_onward = raw[:marker], raw[marker:]
            request_onward = re.sub(b"WARC-Block-Digest: [^\r]+\r\n", b"", request_onward, count=1)
            path.write_bytes(prefix + request_onward)
            with self.assertRaisesRegex(ValueError, "digest"):
                read_archive(path, URL)

    def test_valid_digests_cannot_authorize_a_non_http_response(self):
        """A malformed envelope is not repaired by a valid archive checksum."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.warc.gz"
            self.capture(path, protocol=b"NOTHTTP")
            with self.assertRaisesRegex(ValueError, "protocol"):
                read_archive(path, URL)


if __name__ == "__main__":
    unittest.main()
