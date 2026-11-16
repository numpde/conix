"""Read bounded recorded RPC exchanges without importing the recorder or indexer.

WARC remains the evidence authority. This module retains repeated exchanges
in order; a derived lookup must never silently select one conflicting reply.
"""

import base64
from dataclasses import dataclass
import json
from urllib.parse import urlsplit
import zlib

from warcio.archiveiterator import ArchiveIterator

BODY_LIMIT = 2 * 1024 * 1024
RECORD_LIMIT = BODY_LIMIT + 16384


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Recorded JSON contains duplicate keys.")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("Recorded JSON contains a nonfinite number.")


def document(body):
    """Parse evidence without Python's duplicate-key or nonfinite extensions."""
    return json.loads(body, object_pairs_hook=_object, parse_constant=_nonfinite)


def rpc_key(method, params):
    return (method, json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _decoded(body, headers):
    encodings = [value.lower().strip() for name, value in headers if name.lower() == "content-encoding"]
    require(not encodings or encodings == ["identity"] or encodings == ["gzip"],
            "Recorded HTTP uses an unsupported content encoding.")
    if encodings == ["gzip"]:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            body = decoder.decompress(body, BODY_LIMIT + 1)
        except zlib.error as error:
            raise ValueError("Recorded HTTP gzip payload is malformed.") from error
        require(len(body) <= BODY_LIMIT and decoder.eof and not decoder.unused_data
                and not decoder.unconsumed_tail, "Recorded HTTP gzip payload is incomplete or exceeds its budget.")
    require(len(body) <= BODY_LIMIT, "Recorded HTTP payload exceeds its budget.")
    return body


@dataclass(frozen=True)
class Exchange:
    """One complete conversation, including unsuccessful provider responses."""

    request: dict
    status: int
    body: bytes
    headers: tuple
    started_ns: int
    finished_ns: int

    @property
    def key(self):
        return rpc_key(self.request["method"], self.request["params"])

    def result(self):
        """Required matrix evidence needs a successful, correlated RPC result."""
        require(self.status == 200, "Required RPC evidence has a non-200 HTTP status.")
        reply = document(_decoded(self.body, self.headers).decode("utf-8"))
        require(type(reply) is dict and reply.get("jsonrpc") == "2.0"
                and type(reply.get("id")) is int and reply["id"] == self.request["id"]
                and ("result" in reply) != ("error" in reply),
                "Recorded RPC response has an uncorrelated envelope.")
        require("result" in reply, "Required RPC evidence was rejected by the provider.")
        return reply["result"]


def _metadata(body):
    fields = {}
    for line in body.decode("ascii").splitlines():
        key, separator, value = line.partition(": ")
        require(separator and key not in fields, "Recorded HTTP metadata is ambiguous.")
        fields[key] = value
    require(set(fields) in ({"observed-headers", "observed-monotonic-ns"},
                            {"observed-headers", "observed-monotonic-ns", "observed-reason"}),
            "Recorded HTTP metadata is incomplete.")
    pairs = document(fields["observed-headers"])
    require(type(pairs) is list, "Recorded HTTP header metadata is malformed.")
    for pair in pairs:
        require(type(pair) is list and len(pair) == 2 and all(type(value) is str for value in pair),
                "Recorded HTTP header metadata is malformed.")
        for value in pair:
            base64.b64decode(value, validate=True)
    if "observed-reason" in fields:
        base64.b64decode(fields["observed-reason"], validate=True)
    observed = fields["observed-monotonic-ns"]
    require(observed.isdecimal(), "Recorded HTTP observation time is invalid.")
    return int(observed)


def read_archive(path, source, *, max_exchanges=4096):
    """Admit finalized structure, digests and HTTP framing before returning RPCs.

    Provider error bodies remain evidence. Callers decide whether a complete
    failed observation is an allowed timeline gap or missing required data.
    """
    identities = set()
    requests = {}
    responses = {}
    metadata = {}
    message_ids = set()
    info_count = 0
    with path.open("rb") as archive:
        for count, record in enumerate(ArchiveIterator(archive, check_digests=True), 1):
            require(count <= 4 * max_exchanges + 1, "Recorded archive exceeds its exchange budget.")
            body = record.raw_stream.read(RECORD_LIMIT + 1)
            require(len(body) <= RECORD_LIMIT, "Recorded WARC payload exceeds its budget.")
            require(record.digest_checker.passed is True and not record.digest_checker.problems,
                    "Recorded WARC digest verification failed.")
            require(record.rec_headers.get_header("WARC-Block-Digest")
                    and (record.rec_type == "warcinfo" or record.rec_headers.get_header("WARC-Payload-Digest")),
                    "Recorded WARC lacks required digest declarations.")
            identity = record.rec_headers.get_header("WARC-Record-ID")
            require(identity and identity not in identities, "Recorded WARC identity is absent or repeated.")
            identities.add(identity)
            require(not record.rec_headers.get_header("WARC-Truncated")
                    and not record.rec_headers.get_header("WARC-Segment-Number"),
                    "Recorded WARC exchange is truncated or segmented.")
            kind = record.rec_type
            if kind == "warcinfo":
                info_count += 1
                require(count == 1 and info_count == 1, "Recorded WARC information record is misplaced.")
                continue
            require(kind in ("request", "response", "metadata"), "Unexpected recorded WARC type.")
            require(record.rec_headers.get_header("WARC-Target-URI") == source,
                    "Recorded WARC targets another source.")
            if kind == "metadata":
                linked = record.rec_headers.get_header("WARC-Refers-To")
                require(linked and linked not in metadata, "Recorded WARC metadata linkage is ambiguous.")
                metadata[linked] = _metadata(body)
                continue
            message_ids.add(identity)
            http = record.http_headers
            require(http is not None, "Recorded WARC message lacks HTTP framing.")
            lengths = [value for key, value in http.headers if key.lower() == "content-length"]
            require(lengths == [str(len(body))]
                    and not any(key.lower() == "transfer-encoding" for key, _ in http.headers),
                    "Recorded HTTP framing differs from its stored payload.")
            if kind == "request":
                selected = urlsplit(source)
                target = selected.path or "/"
                if selected.query:
                    target += "?" + selected.query
                require(http.protocol == "POST" and http.statusline == target + " HTTP/1.1",
                        "Recorded request does not POST to the selected source.")
                request = document(_decoded(body, http.headers).decode("utf-8"))
                require(type(request) is dict and request.get("jsonrpc") == "2.0"
                        and type(request.get("id")) is int and type(request.get("method")) is str
                        and type(request.get("params")) is list, "Recorded RPC request is malformed.")
                requests[identity] = request
            else:
                require(http.protocol in ("HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/3"),
                        "Recorded response has an unsupported HTTP protocol.")
                linked = record.rec_headers.get_header("WARC-Concurrent-To")
                require(linked in requests and linked not in responses,
                        "Recorded WARC response linkage is ambiguous.")
                status = http.get_statuscode()
                require(status.isdecimal() and 100 <= int(status) <= 599, "Recorded HTTP status is invalid.")
                responses[linked] = (identity, int(status), body, tuple(http.headers))
    require(info_count == 1 and requests and requests.keys() == responses.keys()
            and metadata.keys() == message_ids, "Recorded WARC contains incomplete conversations.")
    exchanges = []
    for identity, request in requests.items():
        response_id, status, body, headers = responses[identity]
        started, finished = metadata[identity], metadata[response_id]
        require(started <= finished, "Recorded exchange has reversed observation times.")
        exchanges.append(Exchange(request, status, body, headers, started, finished))
    return exchanges
