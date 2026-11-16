"""Capture HTTPX exchanges as portable WARC, before application decoding.

The caller owns the archive file. This transport owns its HTTP transport,
writer, bounded output and serial capture state. It does not interpret RPC.
"""

import base64
from datetime import datetime, timezone
import io
import json
import threading
import time

import httpx
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


class CaptureStopped(RuntimeError):
    """Capture cannot continue without exceeding a grant or losing evidence."""

    def __init__(self, reason):
        allowed = {
            "stopped", "closed", "overlap", "request_limit", "streaming_request",
            "response_limit", "elapsed_budget", "exchange_limit", "archive_limit",
            "archive_write", "cleanup",
        }
        if reason not in allowed:
            raise ValueError("Unknown HTTP capture stop category.")
        super().__init__(f"HTTP capture cannot continue: {reason}.")
        self.reason = reason


class _BoundedSink:
    """Count physical WARC bytes, including compressed framing and short writes."""

    def __init__(self, file, limit):
        self.file = file
        self.limit = limit
        self.written = 0

    def write(self, data):
        if len(data) > self.limit - self.written:
            raise CaptureStopped("archive_limit")
        remaining = memoryview(data)
        while remaining:
            count = self.file.write(remaining)
            if type(count) is not int or not 0 < count <= len(remaining):
                raise OSError("HTTP archive storage did not complete a write.")
            self.written += count
            remaining = remaining[count:]
        return len(data)

    def flush(self):
        self.file.flush()

    def tell(self):
        return self.written


def _date():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _failure(error):
    if isinstance(error, CaptureStopped):
        return "length" if error.reason == "response_limit" else "elapsed_budget"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "disconnect"
    return "unexpected_transport"


class RecordingTransport(httpx.BaseTransport):
    """Store complete encoded responses before returning them to HTTPX.

    Buffering changes caller-visible response timing. Elapsed checks occur
    between reads; the inner transport must have finite I/O timeouts. This
    is an HTTP evidence recorder, not a packet trace or latency benchmark.
    """

    def __init__(self, inner, archive, *, max_request_bytes, max_response_bytes,
                 elapsed_budget_seconds, max_exchanges, max_archive_bytes):
        limits = (max_request_bytes, max_response_bytes, max_exchanges, max_archive_bytes)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("HTTP capture byte and exchange limits must be positive integers.")
        if type(elapsed_budget_seconds) not in (int, float) or not 0 < elapsed_budget_seconds < float("inf"):
            raise ValueError("HTTP capture elapsed budget must be finite and positive.")
        self.inner = inner
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.elapsed_budget_seconds = elapsed_budget_seconds
        self.max_exchanges = max_exchanges
        self.exchanges = 0
        self.stopped = False
        self.closed = False
        self.recording_failures = []
        self.cleanup_failures = []
        self._slot = threading.Lock()
        self._sink = _BoundedSink(archive, max_archive_bytes)
        self._writer = WARCWriter(self._sink, gzip=True, warc_version="1.1")
        try:
            info = self._writer.create_warcinfo_record("capture.warc.gz", {
                "software": "conix HTTP recording transport",
                "format": "WARC File Format 1.1",
                "description": "Encoded HTTP payloads; HTTPX-observed headers are retained as base64 metadata.",
            })
            self._writer.write_record(info)
        except Exception as error:
            self.stopped = True
            self.recording_failures.append(error)
            try:
                self.inner.close()
            except Exception as cleanup:
                self.cleanup_failures.append(cleanup)
            failure = CaptureStopped("archive_write")
            if self.cleanup_failures:
                failure.add_note("Inner HTTP transport retirement is unconfirmed.")
            raise failure from error

    def _metadata(self, url, record_id, *, headers=None, failure=None, observed_reason=None, monotonic_ns=None):
        """Use standard WARC fields; original header bytes remain reversible."""
        fields = []
        if headers is not None:
            pairs = [[base64.b64encode(name).decode("ascii"), base64.b64encode(value).decode("ascii")]
                     for (name, value) in headers]
            fields.append("observed-headers: " + json.dumps(pairs, separators=(",", ":")))
        if observed_reason is not None:
            fields.append("observed-reason: " + base64.b64encode(observed_reason).decode("ascii"))
        if failure is not None:
            fields.append("failure: " + failure)
        if monotonic_ns is not None:
            fields.append("observed-monotonic-ns: " + str(monotonic_ns))
        payload = ("\r\n".join(fields) + "\r\n").encode("ascii")
        record = self._writer.create_warc_record(
            url, "metadata", payload=io.BytesIO(payload), length=len(payload),
            warc_content_type="application/warc-fields",
            warc_headers_dict={"WARC-Refers-To": record_id},
        )
        self._writer.write_record(record)

    def _message(self, request, body, *, response=None, request_id=None, date, failure=None):
        """Reconstruct a readable HTTP envelope around unchanged encoded bytes."""
        recorded_ns = time.monotonic_ns()
        original = list(request.headers.raw if response is None else response.headers.raw)
        headers = [(name.decode("latin-1"), value.decode("latin-1")) for (name, value) in original
                   if name.lower() not in (b"content-length", b"transfer-encoding")]
        headers.append(("Content-Length", str(len(body))))
        observed_reason = None
        if response is None:
            # HTTPX does not expose the negotiated request line before dispatch.
            line = request.method + " " + request.url.raw_path.decode("ascii") + " HTTP/1.1"
            http_headers = StatusAndHeaders(line, headers, is_http_request=True)
            kind = "request"
        else:
            raw_reason = response.extensions.get("reason_phrase", b"")
            try:
                reason = raw_reason.decode("ascii")
            except UnicodeDecodeError:
                # warcio requires an ASCII envelope; retain the original bytes.
                reason = ""
                observed_reason = raw_reason
            protocol = response.extensions.get("http_version", b"HTTP/1.1").decode("ascii")
            http_headers = StatusAndHeaders(f"{response.status_code} {reason}", headers, protocol=protocol)
            kind = "response"
        warc_headers = {"WARC-Date": date}
        if request_id is not None:
            warc_headers["WARC-Concurrent-To"] = request_id
        if failure is not None:
            warc_headers["WARC-Truncated"] = {
                "length": "length", "timeout": "time", "elapsed_budget": "time", "disconnect": "disconnect",
            }.get(failure, "unspecified")
        record = self._writer.create_warc_record(
            str(request.url), kind, payload=io.BytesIO(body), length=len(body), http_headers=http_headers,
            warc_headers_dict=warc_headers,
        )
        self._writer.write_record(record)
        record_id = record.rec_headers.get_header("WARC-Record-ID")
        # warcio may normalize header encoding too; retain observed bytes always.
        self._metadata(str(request.url), record_id, headers=original, failure=failure,
                       observed_reason=observed_reason, monotonic_ns=recorded_ns)
        return record_id

    def handle_request(self, request):
        if not self._slot.acquire(blocking=False):
            raise CaptureStopped("overlap")
        try:
            if self.closed:
                raise CaptureStopped("closed")
            if self.stopped:
                raise CaptureStopped("stopped")
            try:
                body = request.content
            except httpx.RequestNotRead as error:
                self.stopped = True
                raise CaptureStopped("streaming_request") from error
            if len(body) > self.max_request_bytes or self.exchanges >= self.max_exchanges:
                self.stopped = True
                raise CaptureStopped("request_limit" if len(body) > self.max_request_bytes else "exchange_limit")
            self.exchanges += 1
            started = time.monotonic()
            try:
                request_id = self._message(request, body, date=_date())
            except Exception as error:
                self.stopped = True
                self.recording_failures.append(error)
                raise CaptureStopped("archive_write") from error
            return self._exchange(request, request_id, started)
        finally:
            self._slot.release()

    def _exchange(self, request, request_id, started):
        response = None
        body = bytearray()
        primary = None
        arrived = None
        try:
            if time.monotonic() - started >= self.elapsed_budget_seconds:
                raise CaptureStopped("elapsed_budget")
            response = self.inner.handle_request(request)
            arrived = _date()
            for chunk in response.stream:
                remaining = self.max_response_bytes - len(body)
                body.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    raise CaptureStopped("response_limit")
                # Retain observed bytes before classifying a slow completed read.
                # Length takes precedence when both budgets expire on one read.
                if time.monotonic() - started >= self.elapsed_budget_seconds:
                    raise CaptureStopped("elapsed_budget")
            if time.monotonic() - started >= self.elapsed_budget_seconds:
                raise CaptureStopped("elapsed_budget")
        except BaseException as error:
            primary = error
            self.stopped = True

        try:
            if response is None:
                self._metadata(str(request.url), request_id, failure=_failure(primary),
                               monotonic_ns=time.monotonic_ns())
            else:
                self._message(request, bytes(body), response=response, request_id=request_id,
                              date=arrived, failure=_failure(primary) if primary is not None else None)
        except Exception as error:
            self.stopped = True
            self.recording_failures.append(error)
            if primary is None:
                primary = CaptureStopped("archive_write")
                primary.__cause__ = error
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception as error:
                    self.stopped = True
                    self.cleanup_failures.append(error)
                    if primary is None:
                        primary = CaptureStopped("cleanup")
                        primary.__cause__ = error
        if primary is not None:
            raise primary
        return httpx.Response(response.status_code, headers=response.headers.raw,
                              stream=httpx.ByteStream(bytes(body)), extensions=response.extensions)

    def close(self):
        """Refuse active retirement; never replace a preceding capture failure."""
        if not self._slot.acquire(blocking=False):
            raise CaptureStopped("overlap")
        try:
            if self.closed:
                return
            failed_before_close = self.stopped
            self.closed = True
            primary = None
            for action, failures, reason in (
                (self.inner.close, self.cleanup_failures, "cleanup"),
                (self._sink.flush, self.recording_failures, "archive_write"),
            ):
                try:
                    action()
                except Exception as error:
                    self.stopped = True
                    failures.append(error)
                    if primary is None:
                        primary = CaptureStopped(reason)
                        primary.__cause__ = error
            if primary is not None and not failed_before_close:
                raise primary
        finally:
            self._slot.release()
