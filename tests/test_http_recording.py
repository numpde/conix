"""Real HTTP preservation and capture failure contracts, independent of Conix."""

import base64
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import random
import socket
import threading
import time
import unittest

import httpx
from warcio.archiveiterator import ArchiveIterator

from http_recording import CaptureStopped, RecordingTransport


BINARY = bytes(range(256)) * 8
COMPRESSED = gzip.compress(BINARY, mtime=0)
NOISE = random.Random(17).randbytes(8192)


class Provider(BaseHTTPRequestHandler):
    """Produce actual HTTP framing/content that a recorder must not interpret."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/partial":
            self.send_response(200)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"abc")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            self.close_connection = True
            return
        if self.path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
            self.wfile.flush()
            return
        body = {
            "/gzip": COMPRESSED, "/error": b"provider prose EXAMPLEONLY",
            "/invalid": b"{broken json", "/noise": NOISE,
        }.get(self.path, b'  { "result": 7 }\n')
        self.send_response(429 if self.path == "/error" else 200, "\xff" if self.path == "/reason" else None)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Evidence", "first")
        self.send_header("X-Evidence", "\xff")
        if self.path == "/gzip":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        if self.path == "/slow":
            time.sleep(0.04)
        self.wfile.write(body)
        self.wfile.flush()


def records(output):
    """Read raw WARC payloads without payload decompression or EVM machinery."""
    found = []
    for record in ArchiveIterator(io.BytesIO(output.getvalue()), check_digests=True):
        payload = record.raw_stream.read()
        found.append({
            "kind": record.rec_type, "id": record.rec_headers.get_header("WARC-Record-ID"),
            "paired": record.rec_headers.get_header("WARC-Concurrent-To"),
            "refers": record.rec_headers.get_header("WARC-Refers-To"),
            "truncated": record.rec_headers.get_header("WARC-Truncated"),
            "mime": record.content_type, "payload": payload,
            "http": record.http_headers,
        })
        if record.digest_checker.problems:
            raise AssertionError("Recorded WARC digest differs from its stored bytes.")
    return found


def metadata(record):
    fields = dict(line.split(": ", 1) for line in record["payload"].decode("ascii").splitlines())
    if "observed-headers" in fields:
        fields["observed-headers"] = [tuple(base64.b64decode(value) for value in pair)
                                      for pair in json.loads(fields["observed-headers"])]
    return fields


class HTTPRecordingContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls.thread.is_alive():
            raise AssertionError("HTTP fixture did not retire.")

    def recorder(self, output, inner=None, **limits):
        selected = dict(max_request_bytes=16384, max_response_bytes=16384,
                        elapsed_budget_seconds=2, max_exchanges=20, max_archive_bytes=1024 * 1024)
        selected.update(limits)
        return RecordingTransport(inner or httpx.HTTPTransport(retries=0, trust_env=False), output, **selected)

    def test_real_encoded_bodies_errors_repeats_and_raw_headers_survive(self):
        """Capture provider bytes before HTTPX decoding or any RPC admission."""
        output = io.BytesIO()
        recorder = self.recorder(output)
        headers = [(b"X-Request", b"first"), (b"X-Request", b"\xff")]
        payload = b"  { \"id\": 1 }\n"
        paths = ("/gzip", "/error", "/invalid", "/invalid", "/chunked", "/reason")
        with httpx.Client(transport=recorder, trust_env=False, timeout=1) as client:
            responses = [client.post(self.url + path, content=payload, headers=headers) for path in paths]
        self.assertEqual(responses[0].content, BINARY)
        self.assertEqual(responses[1].status_code, 429)
        self.assertEqual(responses[1].content, b"provider prose EXAMPLEONLY")
        self.assertEqual(responses[2].content, b"{broken json")
        saved = records(output)
        requests = [r for r in saved if r["kind"] == "request"]
        replies = [r for r in saved if r["kind"] == "response"]
        self.assertEqual(len(requests), len(paths))
        self.assertEqual([r["payload"] for r in requests], [payload] * len(paths))
        self.assertEqual([r["payload"] for r in replies], [COMPRESSED, b"provider prose EXAMPLEONLY",
                         b"{broken json", b"{broken json", b"abcde", b'  { "result": 7 }\n'])
        for request, reply in zip(requests, replies, strict=True):
            self.assertEqual(reply["paired"], request["id"])
            self.assertIsNone(reply["truncated"])
        self.assertEqual(replies[-2]["http"].get_header("Content-Length"), "5")
        self.assertIsNone(replies[-2]["http"].get_header("Transfer-Encoding"))
        observed = {r["refers"]: metadata(r) for r in saved if r["kind"] == "metadata"}
        self.assertEqual(base64.b64decode(observed[replies[-1]["id"]]["observed-reason"]), b"\xff")
        self.assertEqual([p for p in observed[requests[0]["id"]]["observed-headers"]
                          if p[0].lower() == b"x-request"], headers)
        self.assertEqual([p for p in observed[replies[0]["id"]]["observed-headers"]
                          if p[0].lower() == b"x-evidence"], [(b"X-Evidence", b"first"), (b"X-Evidence", b"\xff")])

    def test_disconnect_preserves_partial_body_and_stops_later_dispatch(self):
        """A disconnected exchange stays incomplete, with the original HTTPX cause."""
        output = io.BytesIO()
        recorder = self.recorder(output)
        with httpx.Client(transport=recorder, trust_env=False, timeout=1) as client:
            with self.assertRaises(httpx.RemoteProtocolError):
                client.post(self.url + "/partial", content=b"read")
            with self.assertRaises(CaptureStopped) as caught:
                client.post(self.url + "/invalid", content=b"read")
            self.assertEqual(caught.exception.reason, "stopped")
        saved = records(output)
        replies = [r for r in saved if r["kind"] == "response"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["payload"], b"abc")
        self.assertEqual(replies[0]["truncated"], "disconnect")
        self.assertEqual(recorder.exchanges, 1)

    def test_byte_and_elapsed_limits_never_return_a_partial_success(self):
        """Limited responses must raise after recording their explicitly partial evidence."""
        for path, limits, reason, truncated in (
            ("/invalid", {"max_response_bytes": 3}, "response_limit", "length"),
            ("/slow", {"elapsed_budget_seconds": 0.01}, "elapsed_budget", "time"),
        ):
            with self.subTest(reason=reason):
                output = io.BytesIO()
                recorder = self.recorder(output, **limits)
                with httpx.Client(transport=recorder, trust_env=False, timeout=1) as client:
                    with self.assertRaises(CaptureStopped) as caught:
                        client.post(self.url + path, content=b"read")
                self.assertEqual(caught.exception.reason, reason)
                reply = next(r for r in records(output) if r["kind"] == "response")
                self.assertEqual(reply["truncated"], truncated)
                expected = b"{br" if reason == "response_limit" else b'  { "result": 7 }\n'
                self.assertEqual(reply["payload"], expected)
                self.assertTrue(recorder.stopped)

    def test_archive_limit_includes_compression_framing_and_stops_dispatch(self):
        """An incompressible response cannot push physical output beyond its grant."""
        output = io.BytesIO()
        recorder = self.recorder(output, max_archive_bytes=2000)
        with httpx.Client(transport=recorder, trust_env=False, timeout=1) as client:
            with self.assertRaises(CaptureStopped):
                client.post(self.url + "/noise", content=b"read")
            with self.assertRaises(CaptureStopped):
                client.post(self.url + "/invalid", content=b"read")
        self.assertLessEqual(len(output.getvalue()), 2000)
        self.assertEqual(recorder.exchanges, 1)
        self.assertTrue(recorder.recording_failures)

    def test_short_writes_complete_and_zero_writes_fail_before_dispatch(self):
        """A successful writer call must mean every encoded byte was accepted."""
        class ShortSink(io.BytesIO):
            zero = False

            def write(self, data):
                return 0 if self.zero else super().write(data[:3])

        output = ShortSink()
        recorder = self.recorder(output)
        with httpx.Client(transport=recorder, trust_env=False, timeout=1) as client:
            client.post(self.url + "/invalid", content=b"read")
        self.assertEqual(next(r["payload"] for r in records(output) if r["kind"] == "response"), b"{broken json")
        output = ShortSink()
        recorder = self.recorder(output)
        output.zero = True
        with httpx.Client(transport=recorder, trust_env=False, timeout=1) as client:
            with self.assertRaises(CaptureStopped):
                client.post(self.url + "/invalid", content=b"read")
        self.assertTrue(recorder.stopped)
        self.assertTrue(recorder.recording_failures)

    def test_transport_failure_remains_primary_when_recording_and_close_fail(self):
        """Failure reporting must not replace the first cause with storage or cleanup."""
        primary = httpx.ReadError("original read failed")

        class BrokenArchive(io.BytesIO):
            broken = False

            def write(self, data):
                if self.broken:
                    raise OSError("EXAMPLEONLY private archive detail")
                return super().write(data)

        class BrokenBody(httpx.SyncByteStream):
            def __iter__(self):
                yield b"partial"
                raise primary

            def close(self):
                raise PermissionError("EXAMPLEONLY private cleanup detail")

        output = BrokenArchive()

        class Inner(httpx.BaseTransport):
            def handle_request(self, request):
                output.broken = True
                return httpx.Response(200, stream=BrokenBody())

        recorder = self.recorder(output, inner=Inner())
        with httpx.Client(transport=recorder, trust_env=False) as client:
            with self.assertRaises(httpx.ReadError) as caught:
                client.post(self.url, content=b"read")
        self.assertIs(caught.exception, primary)
        self.assertEqual(type(recorder.recording_failures[0]), OSError)
        self.assertEqual(type(recorder.cleanup_failures[0]), PermissionError)
        self.assertTrue(recorder.stopped)

    def test_no_response_records_failure_without_inventing_status(self):
        """A connection timeout cannot be represented as a provider HTTP response."""
        class Inner(httpx.BaseTransport):
            def handle_request(self, request):
                raise httpx.ConnectTimeout("source unavailable")

        output = io.BytesIO()
        recorder = self.recorder(output, inner=Inner())
        with httpx.Client(transport=recorder, trust_env=False) as client:
            with self.assertRaises(httpx.ConnectTimeout):
                client.post(self.url, content=b"read")
        saved = records(output)
        self.assertFalse(any(r["kind"] == "response" for r in saved))
        failure = next(metadata(r) for r in saved if r["kind"] == "metadata" and b"failure:" in r["payload"])
        self.assertEqual(failure["failure"], "timeout")

    def test_overlap_is_refused_before_dispatch_without_breaking_active_capture(self):
        """One active request owns the writer; a concurrent request must not enter it."""
        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        class Inner(httpx.BaseTransport):
            def handle_request(self, request):
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("Overlap fixture was not released.")
                return httpx.Response(200, stream=httpx.ByteStream(b"complete"))

        output = io.BytesIO()
        recorder = self.recorder(output, inner=Inner())
        request = httpx.Request("POST", self.url, content=b"read")

        def first():
            try:
                outcomes.append(recorder.handle_request(request))
            except BaseException as error:
                outcomes.append(error)

        worker = threading.Thread(target=first)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaises(CaptureStopped) as caught:
                recorder.handle_request(request)
            self.assertEqual(caught.exception.reason, "overlap")
            with self.assertRaises(CaptureStopped):
                recorder.close()
            self.assertFalse(recorder.closed)
        finally:
            release.set()
            worker.join(timeout=5)
            recorder.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], httpx.Response)
        self.assertEqual(recorder.exchanges, 1)


    def test_elapsed_budget_is_checked_before_dispatch_after_slow_archive_write(self):
        """A slow request record must not grant a fresh network budget afterwards."""
        class SlowArchive(io.BytesIO):
            slow = False

            def flush(self):
                if self.slow:
                    time.sleep(0.03)
                super().flush()

        class Inner(httpx.BaseTransport):
            calls = 0

            def handle_request(self, request):
                self.calls += 1
                return httpx.Response(200, stream=httpx.ByteStream(b"complete"))

        output = SlowArchive()
        inner = Inner()
        recorder = self.recorder(output, inner=inner, elapsed_budget_seconds=0.01)
        output.slow = True
        with httpx.Client(transport=recorder, trust_env=False) as client:
            with self.assertRaises(CaptureStopped) as caught:
                client.post(self.url, content=b"read")
        self.assertEqual(caught.exception.reason, "elapsed_budget")
        self.assertEqual(inner.calls, 0)
        self.assertFalse(any(r["kind"] == "response" for r in records(output)))

    def test_archive_failure_after_http_success_retains_its_storage_cause(self):
        """The public stop category must retain the actual storage cause in process."""
        failure = OSError("EXAMPLEONLY storage cause")

        class BrokenArchive(io.BytesIO):
            broken = False

            def write(self, data):
                if self.broken:
                    raise failure
                return super().write(data)

        output = BrokenArchive()

        class Inner(httpx.BaseTransport):
            def handle_request(self, request):
                output.broken = True
                return httpx.Response(200, stream=httpx.ByteStream(b"complete"))

        recorder = self.recorder(output, inner=Inner())
        with httpx.Client(transport=recorder, trust_env=False) as client:
            with self.assertRaises(CaptureStopped) as caught:
                client.post(self.url, content=b"read")
        self.assertEqual(caught.exception.reason, "archive_write")
        self.assertIs(caught.exception.__cause__, failure)
        self.assertNotIn("EXAMPLEONLY", str(caught.exception))

    def test_request_and_exchange_limits_refuse_before_inner_dispatch(self):
        """Admission limits cannot consume a stream or send an extra request."""
        class Inner(httpx.BaseTransport):
            calls = 0

            def handle_request(self, request):
                self.calls += 1
                return httpx.Response(200, stream=httpx.ByteStream(b"complete"))

        for scenario in ("request_limit", "streaming_request", "exchange_limit"):
            with self.subTest(scenario=scenario):
                inner = Inner()
                output = io.BytesIO()
                recorder = self.recorder(output, inner=inner, max_request_bytes=3, max_exchanges=1)
                touched = []

                def chunks():
                    touched.append(True)
                    yield b"read"

                request = httpx.Request("POST", self.url, content=chunks() if scenario == "streaming_request" else b"read")
                if scenario == "exchange_limit":
                    recorder.handle_request(httpx.Request("POST", self.url, content=b"ok")).close()
                try:
                    with self.assertRaises(CaptureStopped) as caught:
                        recorder.handle_request(request if scenario != "exchange_limit" else
                                                httpx.Request("POST", self.url, content=b"ok"))
                    self.assertEqual(caught.exception.reason, scenario)
                finally:
                    recorder.close()
                self.assertEqual(inner.calls, 1 if scenario == "exchange_limit" else 0)
                self.assertEqual(touched, [])
