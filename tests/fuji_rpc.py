"""Test-only fixed-upstream, read-method gateway for the Fuji campaign.

Historical mode exposes a labelled bounded head using genuine source blocks.
Live mode forwards the genuine moving head. No request can select an upstream
or submit a transaction. This is transport policy, not indexing evidence.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import socket
from urllib.parse import urlsplit
import os
from pathlib import Path
import re
import threading
import time

import httpx

SOURCE = json.loads((Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json").read_text())
READ_METHODS = frozenset({
    "eth_chainId", "eth_getBlockByNumber", "eth_getBlockByHash", "eth_getLogs",
    "eth_getCode", "eth_call", "eth_getTransactionReceipt",
})
MAX_BYTES = 2 * 1024 * 1024


class GatewayRefusal(ValueError):
    """The test gateway cannot admit this request."""


class UpstreamFailure(RuntimeError):
    """The admitted read failed upstream; provider prose remains private."""

    def __init__(self, cause, status=None):
        super().__init__("Selected Fuji upstream read failed.")
        self.cause = cause
        self.status = status


def admit(raw):
    """Only bounded, single JSON-RPC reads reach the fixed public endpoint."""
    if not 0 < len(raw) <= 16384:
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


def bounded_view(request, historical):
    """Project only latest/numbered headers; never invent hashes or finality."""
    if not historical or request["method"] != "eth_getBlockByNumber":
        return request, False
    params = request["params"]
    if len(params) != 2 or params[1] is not False:
        raise GatewayRefusal("Historical view requires a header-only block request.")
    selector = params[0]
    ceiling = SOURCE["initial_interval"]["end_block"]
    if selector == "latest":
        return {**request, "params": [hex(ceiling), False]}, False
    if type(selector) is not str or re.fullmatch(r"0x[0-9a-fA-F]+", selector) is None:
        raise GatewayRefusal("Historical view does not assert tagged finality.")
    return request, int(selector, 16) > ceiling


def forward(client, request):
    """Do not retry, follow redirects or expose arbitrary provider error prose."""
    deadline = time.monotonic() + 15
    with client.stream("POST", SOURCE["rpc_url"], json=request) as response:
        if response.status_code != 200:
            raise UpstreamFailure("http_status", response.status_code)
        body = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise UpstreamFailure("deadline")
            body.extend(chunk)
            if len(body) > MAX_BYTES:
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
            "code": code, "message": "Selected Fuji provider rejected this read.",
        }}
    if "result" not in result:
        raise UpstreamFailure("reply_result")
    return result


def upstream_addresses():
    """Resolve only the selected public upstream for the confined TCP control."""
    try:
        addresses = socket.getaddrinfo(urlsplit(SOURCE["rpc_url"]).hostname, 443, type=socket.SOCK_STREAM)
        selected = list(dict.fromkeys(str(ipaddress.ip_address(value[4][0])) for value in addresses))
        if not selected or len(selected) > 8 or any(not ipaddress.ip_address(value).is_global for value in selected):
            raise ValueError("Selected upstream has no bounded public address evidence.")
        return selected
    except (OSError, ValueError) as error:
        raise UpstreamFailure("dns_evidence") from error


def serve():
    """Serve only the fixture's internal peers with finite request resources."""
    mode = os.environ.get("CONIX_FUJI_VIEW")
    if mode not in ("historical", "live"):
        raise SystemExit("Fuji gateway requires an explicit historical or live view.")
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
                    self.reply(200, {"addresses": upstream_addresses()})
                except UpstreamFailure:
                    self.reply(502, {"error": "Selected upstream DNS evidence is unavailable."})
            else:
                self.reply(200 if self.path == "/healthz" else 404, {"view": mode})

        def do_POST(self):
            self.connection.settimeout(12)
            if not capacity.acquire(blocking=False):
                self.reply(503, {"error": "Gateway read capacity is occupied."})
                return
            try:
                length = self.headers.get("Content-Length", "")
                if self.path != "/" or not length.isdecimal() or not 0 < int(length) <= 16384:
                    raise GatewayRefusal("Gateway request framing is not admitted.")
                request = admit(self.rfile.read(int(length)))
                selected, absent = bounded_view(request, mode == "historical")
                result = {"jsonrpc": "2.0", "id": request["id"], "result": None} if absent else forward(client, selected)
                self.reply(200, result)
            except GatewayRefusal:
                self.reply(403, {"error": "Fuji gateway refused this request.", "origin": "admission"})
            except UpstreamFailure as error:
                document = {"error": "Fuji upstream read failed.", "origin": "upstream", "cause": error.cause}
                if error.status is not None:
                    document["status"] = error.status
                self.reply(502, document)
            except Exception as error:
                # Type is bounded source-level evidence; never stringify values.
                name = type(error).__name__
                name = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "UnexpectedError"
                self.reply(502, {"error": "Fuji gateway read failed.", "origin": "upstream", "cause": name})
            finally:
                capacity.release()

    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()
        client.close()


if __name__ == "__main__":
    serve()
