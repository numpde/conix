"""Offline Stage C playback of an admitted, immutable Stage B WARC workload.

No external HTTP transport exists. The original Writer and Reader, not this
test-only server, own indexing, decoding, journal publication and SQL queries.
"""
import argparse
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tarfile
import threading
import time

from conix.core import _block
from conix.process_diagnostic import emit_unexpected
from stress_archive import _decoded, document, read_archive, rpc_key
from stress_complete import load, verify_completion
from stress_protocol import BUNDLE_INPUTS

REPLAY_FILES = {**BUNDLE_INPUTS, "completion.warc.gz": 64 * 1048576,
                "admission.json": 2 * 1048576}
MAX_REQUEST_BYTES = 16384
MAX_REPLY_BYTES = 2 * 1048576
FILE_ROOT = Path("/evidence")


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unpack(stream, folder):
    """Grant only exact owned filenames; never unpack paths from archive metadata."""
    seen = set()
    with tarfile.open(fileobj=stream, mode="r|") as source:
        for member in source:
            require(member.name in REPLAY_FILES and member.name not in seen and member.isfile()
                    and 0 < member.size <= REPLAY_FILES[member.name],
                    "Replay transport has an unauthorized or oversized member.")
            seen.add(member.name)
            remaining = member.size
            member_stream = source.extractfile(member)
            with (folder / member.name).open("xb") as target:
                while remaining:
                    block = member_stream.read(min(65536, remaining))
                    require(block, "Replay transport ended within a member.")
                    target.write(block)
                    remaining -= len(block)
    require(seen == set(REPLAY_FILES), "Replay transport lacks exact admitted files.")


def admitted(folder):
    """Revalidate both producer receipts and the canonical completion matrix."""
    admission = document((folder / "admission.json").read_bytes())
    require(type(admission) is dict and admission.get("status") == "complete"
            and admission.get("phase") == "completed" and not admission.get("cleanup"),
            "Stage B admission is absent or unavailable.")
    catalog, manifest, application, polls, hashes = load(folder)
    require(admission.get("input_sha256") == hashes
            and admission.get("source_id") == catalog.source_id.hex()
            and admission.get("capture_revision") == manifest["revision"]
            and admission.get("completion_sha256") == sha256(folder / "completion.warc.gz"),
            "Replay files differ from their admitted source, ABI, archive or catalog hashes.")
    verified = asyncio.run(verify_completion(folder, catalog, application, polls))
    require(admission.get("inspection", {}).get("status") == "verified"
            and admission["inspection"]["completion_sha256"] == verified["completion_sha256"],
            "Stage B independent verification is absent.")
    for field in ("start_block", "final_block", "headers", "selectors", "log_cells", "rechecks"):
        require(admission["coverage"][field] == verified["coverage"][field],
                "Rebuilt WARC coverage differs from the selected Stage B admission.")
    require(admission["coverage"]["completion_exchanges"] == verified["completion_exchanges"],
            "Completion WARC exchange count differs from Stage B.")
    completion = read_archive(folder / "completion.warc.gz", catalog.chain.rpc_url,
                              max_exchanges=16384)
    return catalog, manifest, application + completion, polls, admission


def selected_event(record):
    """Raw event identity, never a second ABI decoder or indexing algorithm."""
    fields = ("address", "blockHash", "blockNumber", "transactionHash",
              "transactionIndex", "logIndex", "topics", "data")
    require(type(record) is dict and all(field in record for field in fields)
            and type(record["topics"]) is list,
            "Admitted selected-log reply lacks raw event identity.")
    return (record["address"].lower(), record["blockHash"].lower(),
            int(record["blockNumber"], 16), record["transactionHash"].lower(),
            int(record["transactionIndex"], 16), int(record["logIndex"], 16),
            tuple(topic.lower() for topic in record["topics"]), record["data"].lower())


class ReplayEvidence:
    """Immutable RPC result lookup derived exclusively from admitted WARC."""

    def __init__(self, folder):
        self.catalog, self.manifest, exchanges, polls, self.admission = admitted(folder)
        self.public_source = self.catalog.chain.rpc_url
        self.start = self.catalog.chain.start_block
        self.final = polls[-1]["header"]["height"]
        self.results = {}
        self.envelopes = {}
        for exchange in exchanges:
            if exchange.key == rpc_key("eth_getBlockByNumber", ["latest", False]):
                continue  # Observed head is a time series, not a reusable result.
            try:
                value = exchange.result()
            except ValueError:
                # A failed, non-matrix capture is not an immutable RPC answer.
                continue
            if exchange.key[0] == "eth_getBlockByNumber" and value is None:
                # A numbered header above the live provider's head was
                # genuinely absent at the time of capture. It is not timeless
                # negative evidence: the later admitted header is immutable
                # once it exists. Replay derives future null from its own
                # visible-head schedule instead of caching the old absence.
                continue
            envelope = document(_decoded(exchange.body, exchange.headers).decode("utf-8"))
            require(type(envelope) is dict and type(envelope.get("id")) is int
                    and envelope["id"] == exchange.request["id"],
                    "Recorded immutable reply does not correlate to its exact request.")
            key = exchange.key
            # Recorded IDs differ across repeated immutable reads. Keep every
            # other member of the upstream response, not just result bytes.
            immutable_envelope = dict(envelope, id=None)
            if key in self.results:
                require(self.results[key] == value
                        and self.envelopes[key] == immutable_envelope,
                        "Repeated immutable RPC evidence conflicts.")
            else:
                self.results[key] = value
                self.envelopes[key] = immutable_envelope
        require(rpc_key("eth_chainId", []) in self.results
                and int(self.results[rpc_key("eth_chainId", [])], 16) == self.catalog.chain.chain_id,
                "Admitted chain identity is missing.")
        self.headers = {}
        for height in sorted({0, *range(max(self.start - 1, 0), self.final + 1)}):
            key = rpc_key("eth_getBlockByNumber", [hex(height), False])
            require(key in self.results, "Admitted numbered-header evidence is missing.")
            raw = self.results[key]
            block = _block(raw, height).header
            self.headers[height] = raw
            require(block.number == height, "Admitted header identity changed.")
        self.by_hash = {raw["hash"].lower(): height for height, raw in self.headers.items()}
        self.expected = Counter()
        for height in range(self.start, self.final + 1):
            raw = self.headers[height]
            for contract in self.catalog.contracts:
                for event in contract.descriptor.events:
                    params = [{"blockHash": raw["hash"].lower(), "address": "0x" + contract.address.hex(),
                               "topics": ["0x" + event.topic0.hex()]}]
                    key = rpc_key("eth_getLogs", params)
                    require(key in self.results and type(self.results[key]) is list,
                            "Admitted selected-log response is missing, even when empty.")
                    for row in self.results[key]:
                        self.expected[selected_event(row)] += 1
        samples = self.manifest["observer"]["samples"]
        # A head returned by an HTTP poll is observed when the reply has
        # completed, not retroactively when its request started. This timing
        # projection avoids giving replay earlier knowledge than the observer.
        first = samples[0]["elapsed_end_ns"]
        self.timeline = []
        for sample in samples:
            if sample["status"] == "observed":
                elapsed = sample["elapsed_end_ns"] - first
                require(type(elapsed) is int and elapsed >= 0 and
                        self.start <= sample["header"]["height"] <= self.final,
                        "Admitted head schedule has invalid elapsed time or height.")
                self.timeline.append((elapsed, sample["header"]["height"]))
        require(self.timeline and self.timeline[0][0] == 0
                and self.timeline[-1][1] == self.final
                and all(self.timeline[i][0] < self.timeline[i+1][0]
                        and self.timeline[i][1] <= self.timeline[i+1][1]
                        for i in range(len(self.timeline)-1)),
                "Admitted replay head observations are out of order.")
        self.window_ns = self.timeline[-1][0]
        self.gaps = sum(sample["status"] != "observed" for sample in samples)
        self.clock_uncertainty_ns = self.manifest["clocks"]["observer"]["uncertainty_ns"]

    def response(self, method, params, head):
        """Only the original consumer's exact RPC forms are replayable."""
        require(type(method) is str and type(params) is list, "Unsupported RPC envelope.")
        if method == "eth_chainId":
            require(params == [], "Unsupported chain-ID parameters.")
            return self.results[rpc_key(method, params)]
        if method == "eth_getBlockByNumber":
            require(len(params) == 2 and params[1] is False
                    and type(params[0]) is str, "Unsupported numbered-block selector.")
            if params[0] == "latest":
                return self.headers[head]
            selector = params[0]
            require(selector.startswith("0x") and len(selector) > 2
                    and all(c in "0123456789abcdef" for c in selector[2:])
                    and selector == hex(int(selector, 16)), "Unsupported block tag or number.")
            height = int(selector, 16)
            if height > head:
                return None  # Explicit timing projection, never missing history.
            require(height in self.headers, "Numbered block below visible head has no evidence.")
            return self.headers[height]
        if method == "eth_getLogs":
            require(len(params) == 1 and type(params[0]) is dict
                    and set(params[0]) == {"blockHash", "address", "topics"}
                    and type(params[0]["blockHash"]) is str
                    and type(params[0]["address"]) is str
                    and type(params[0]["topics"]) is list
                    and len(params[0]["topics"]) == 1,
                    "Unsupported log query form.")
            key = rpc_key(method, params)
            height = self.by_hash.get(params[0]["blockHash"].lower())
            require(height is not None and height <= head
                    and key in self.results, "Selected log evidence is unavailable at this head.")
            return self.results[key]
        raise ValueError("Unsupported original-consumer RPC method.")


    def served_envelope(self, method, params, head, request_id):
        """Preserve all captured envelope members except the caller's new ID."""
        value = self.response(method, params, head)
        if value is None:
            # An above-head numbered null is a derived timing projection.
            return {"jsonrpc": "2.0", "id": request_id, "result": None}
        key = (rpc_key("eth_getBlockByNumber", [hex(head), False])
               if method == "eth_getBlockByNumber" and params[0] == "latest"
               else rpc_key(method, params))
        require(key in self.envelopes,
                "Immutable reply envelope is absent from admitted WARC evidence.")
        return dict(self.envelopes[key], id=request_id)


class HeadSchedule:
    """Time-only head advancement; requests cannot move the recorded chain."""

    def __init__(self, evidence, *, monotonic=time.monotonic_ns):
        self.evidence = evidence
        self.clock = monotonic
        self.release_ns = None

    def release(self):
        require(self.release_ns is None, "Replay was already released.")
        self.release_ns = self.clock()
        return self.release_ns

    def view(self):
        now = self.clock()
        elapsed = 0 if self.release_ns is None else max(0, now - self.release_ns)
        head = self.evidence.timeline[0][1]
        if self.release_ns is not None:
            for at, observed in self.evidence.timeline[1:]:
                if elapsed < at:
                    break
                head = observed
        return {"released": self.release_ns is not None, "elapsed_ns": elapsed, "head": head,
                "frozen": self.release_ns is not None and elapsed >= self.evidence.window_ns}


class ReplayHTTP(HTTPServer):
    def __init__(self, address, evidence, delay_ms):
        self.evidence = evidence
        self.schedule = HeadSchedule(evidence)
        self.delay_ms = delay_ms
        self.requests = 0
        self.errors = 0
        self.wait_ns = 0
        self.first_rpc_ns = None
        self.pre_release_requests = 0
        super().__init__(address, ReplayHandler)

    def status(self):
        view = self.schedule.view()
        return dict(view, requests=self.requests, errors=self.errors,
                    actual_wait_ns=self.wait_ns,
                    first_rpc_elapsed_ns=self.first_rpc_ns, delay_ms=self.delay_ms,
                    pre_release_requests=self.pre_release_requests,
                    window_ns=self.evidence.window_ns, sampled_gaps=self.evidence.gaps,
                    clock_uncertainty_ns=self.evidence.clock_uncertainty_ns)


class ReplayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, code, payload):
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("ascii")
        require(len(body) <= MAX_REPLY_BYTES, "Replay reply exceeded its bounded output.")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.reply(200, {"status": "ready"})
        elif self.path == "/status":
            self.reply(200, self.server.status())
        else:
            self.reply(404, {"error": "Unsupported control path."})

    def do_POST(self):
        if self.path == "/release":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self.reply(403, {"error": "Release requires owned loopback control."})
                return
            try:
                started = self.server.schedule.release()
                self.reply(200, {"released_ns": started})
            except ValueError:
                self.reply(409, {"error": "Replay is already released."})
            return
        if self.path != "/":
            self.reply(404, {"error": "Unsupported RPC path."})
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or not 0 < int(length) <= MAX_REQUEST_BYTES:
            self.server.errors += 1
            self.reply(400, {"error": "RPC input exceeds its admitted framing."})
            return
        try:
            request = document(self.rfile.read(int(length)))
            require(type(request) is dict and set(request) == {"jsonrpc", "id", "method", "params"}
                    and request["jsonrpc"] == "2.0" and type(request["id"]) is int
                    and 0 <= request["id"] < 2**63, "Unsupported JSON-RPC request.")
            view = self.server.schedule.view()
            payload = self.server.evidence.served_envelope(
                request["method"], request["params"], view["head"], request["id"])
        except (ValueError, KeyError, TypeError):
            self.server.errors += 1
            self.reply(409, {"error": "Exact admitted RPC evidence is unavailable."})
            return
        if self.server.first_rpc_ns is None:
            self.server.first_rpc_ns = self.server.schedule.view()["elapsed_ns"]
        if not view["released"]:
            self.server.pre_release_requests += 1
        self.server.requests += 1
        if self.server.delay_ms:
            before = time.monotonic_ns()
            time.sleep(self.server.delay_ms / 1000)
            self.server.wait_ns += time.monotonic_ns() - before
        self.reply(200, payload)


def control(operation):
    from urllib.request import Request, urlopen
    require(operation in ("status", "release"), "Unknown replay control.")
    url = "http://127.0.0.1:8080/" + operation
    method = "POST" if operation == "release" else "GET"
    with urlopen(Request(url, method=method), timeout=5) as result:
        payload = result.read(16385)
    require(len(payload) <= 16384, "Replay control exceeded its budget.")
    print(payload.decode("ascii"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "serve", "status", "release"))
    parser.add_argument("--delay-ms", type=int, default=0)
    args = parser.parse_args()
    if args.operation == "stage":
        FILE_ROOT.mkdir(mode=0o700, exist_ok=True)
        require(not list(FILE_ROOT.iterdir()), "Replay volume must be empty before admission.")
        unpack(sys.stdin.buffer, FILE_ROOT)
        evidence = ReplayEvidence(FILE_ROOT)
        print(json.dumps({"status": "admitted", "source_id": evidence.catalog.source_id.hex(),
                          "start": evidence.start, "final": evidence.final,
                          "window_ns": evidence.window_ns}))
        return
    if args.operation in ("status", "release"):
        return control(args.operation)
    require(type(args.delay_ms) is int and 0 <= args.delay_ms <= 5000,
            "Replay latency must be one finite fixed delay.")
    evidence = ReplayEvidence(FILE_ROOT)
    server = ReplayHTTP(("0.0.0.0", 8080), evidence, args.delay_ms)
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM,
                  lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as error:
        emit_unexpected(phase="verification", error=error, effect="unconfirmed")
        raise SystemExit(1) from None
