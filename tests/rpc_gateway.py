"""Test-only JSON-RPC gateway with one explicitly configured HTTPS upstream.

Only admitted reads reach the upstream. An optional header ceiling projects
head discovery for historical campaigns; other reads remain genuine upstream
reads. This transport policy does not establish indexing or finality evidence.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import socket
from urllib.parse import urlsplit
import re
import threading
import time

import httpx

READ_METHODS = frozenset({
    "eth_chainId", "eth_getBlockByNumber", "eth_getBlockByHash", "eth_getLogs",
    "eth_getCode", "eth_call", "eth_getTransactionReceipt",
})
HTTPS_PORT = 443
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class GatewayRefusal(ValueError):
    """The test gateway cannot admit this request."""


class UpstreamFailure(RuntimeError):
    """The admitted read failed upstream; provider prose remains private."""

    def __init__(self, cause, status=None):
        super().__init__("Selected upstream read failed.")
        self.cause = cause
        self.status = status


def admit(raw):
    """Only bounded, single JSON-RPC reads reach the fixed public endpoint."""
    if not 0 < len(raw) <= MAX_REQUEST_BYTES:
        raise GatewayRefusal("Gateway request exceeds the admitted size.")
    try:
        request = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise GatewayRefusal("Gateway request is not JSON.") from error
    if (
        type(request) is not dict or set(request) != {"jsonrpc", "id", "method", "params"}
        or request["jsonrpc"] != "2.0" or type(request["id"]) is not int
        or not 0 <= request["id"] < 2**63 or type(request["method"]) is not str
        or request["method"] not in READ_METHODS
        or type(request["params"]) is not list or len(request["params"]) > 8
    ):
        raise GatewayRefusal("Gateway permits only single admitted JSON-RPC reads.")
    return request


def admit_configuration(upstream_url, head_ceiling):
    """Reject ambiguous startup grants before opening a client or listener.

    DNS evidence and the existing direct-egress probe both refer to port 443.
    Credentials and alternate transports require a separate explicit contract.
    """
    if type(upstream_url) is not str or not 0 < len(upstream_url) <= 2048:
        raise ValueError("RPC gateway requires one explicit HTTPS upstream on port 443.")
    try:
        endpoint = urlsplit(upstream_url)
        admitted = (
            endpoint.scheme == "https" and endpoint.hostname
            and endpoint.port in (None, HTTPS_PORT) and endpoint.username is None
            and endpoint.password is None and not endpoint.fragment
            and not endpoint.query and not any(character.isspace() or ord(character) < 32
                                               for character in upstream_url)
        )
    except ValueError:
        admitted = False
    if not admitted:
        raise ValueError("RPC gateway requires a credential-free HTTPS upstream on port 443 without a query or fragment.")
    if head_ceiling is not None and (type(head_ceiling) is not int or head_ceiling < 0):
        raise ValueError("RPC gateway header ceiling must be a nonnegative block number or None for a live head.")


def bounded_view(request, *, head_ceiling):
    """Project only latest/numbered headers; never invent hashes or finality."""
    if head_ceiling is None or request["method"] != "eth_getBlockByNumber":
        return request, False
    params = request["params"]
    if len(params) != 2 or params[1] is not False:
        raise GatewayRefusal("Historical view requires a header-only block request.")
    selector = params[0]
    ceiling = head_ceiling
    if selector == "latest":
        return {**request, "params": [hex(ceiling), False]}, False
    if type(selector) is not str or re.fullmatch(r"0x[0-9a-fA-F]+", selector) is None:
        raise GatewayRefusal("Historical view does not assert tagged finality.")
    return request, int(selector, 16) > ceiling


def forward(client, request, *, upstream_url):
    """Do not retry, follow redirects or expose arbitrary provider error prose."""
    deadline = time.monotonic() + 15
    with client.stream("POST", upstream_url, json=request) as response:
        if response.status_code != 200:
            raise UpstreamFailure("http_status", response.status_code)
        body = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise UpstreamFailure("deadline")
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise UpstreamFailure("reply_size")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise UpstreamFailure("reply_json") from error
    if type(result) is not dict or result.get("id") != request["id"]:
        raise UpstreamFailure("reply_identity")
    if "error" in result:
        code = result["error"].get("code") if type(result["error"]) is dict else None
        code = code if type(code) is int and abs(code) < 2**31 else -32000
        return {"jsonrpc": "2.0", "id": request["id"], "error": {
            "code": code, "message": "Selected provider rejected this read.",
        }}
    if "result" not in result:
        raise UpstreamFailure("reply_result")
    return result


def upstream_addresses(*, upstream_url):
    """Resolve only the selected public upstream for the confined TCP control."""
    try:
        addresses = socket.getaddrinfo(urlsplit(upstream_url).hostname, HTTPS_PORT, type=socket.SOCK_STREAM)
        selected = list(dict.fromkeys(str(ipaddress.ip_address(value[4][0])) for value in addresses))
        if not selected or len(selected) > 8 or any(not ipaddress.ip_address(value).is_global for value in selected):
            raise ValueError("Selected upstream has no bounded public address evidence.")
        return selected
    except (OSError, ValueError) as error:
        raise UpstreamFailure("dns_evidence") from error


def serve(*, upstream_url, head_ceiling=None):
    """Serve internal peers using caller-owned source configuration.

    None exposes the live head. A number projects header discovery only;
    it does not constrain logs, calls, receipts or hash-selected headers.
    """
    admit_configuration(upstream_url, head_ceiling)
    mode = "live" if head_ceiling is None else "historical"
    capacity = threading.BoundedSemaphore(4)
    client = httpx.Client(trust_env=False, follow_redirects=False, timeout=10)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(12)

        def log_message(self, *_args):
            pass  # Requests, response bodies and provider prose never enter logs.

        def reply(self, status, document):
            payload = json.dumps(document, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/upstream-addresses":
                try:
                    self.reply(200, {"addresses": upstream_addresses(upstream_url=upstream_url)})
                except UpstreamFailure:
                    self.reply(502, {"error": "Selected upstream DNS evidence is unavailable."})
            else:
                self.reply(200 if self.path == "/healthz" else 404, {"view": mode})

        def do_POST(self):
            if not capacity.acquire(blocking=False):
                self.reply(503, {"error": "Gateway read capacity is occupied."})
                return
            try:
                length = self.headers.get("Content-Length", "")
                if self.path != "/" or not length.isdecimal() or not 0 < int(length) <= MAX_REQUEST_BYTES:
                    raise GatewayRefusal("Gateway request framing is not admitted.")
                request = admit(self.rfile.read(int(length)))
                selected, absent = bounded_view(request, head_ceiling=head_ceiling)
                result = {"jsonrpc": "2.0", "id": request["id"], "result": None} if absent else forward(client, selected, upstream_url=upstream_url)
                self.reply(200, result)
            except GatewayRefusal:
                self.reply(403, {"error": "RPC gateway refused this request.", "origin": "admission"})
            except UpstreamFailure as error:
                document = {"error": "RPC upstream read failed.", "origin": "upstream", "cause": error.cause}
                if error.status is not None:
                    document["status"] = error.status
                self.reply(502, document)
            except Exception as error:
                # Type is bounded source-level evidence; never stringify values.
                name = type(error).__name__
                name = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "UnexpectedError"
                self.reply(502, {"error": "RPC gateway read failed.", "origin": "upstream", "cause": name})
            finally:
                capacity.release()

    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()
        client.close()
