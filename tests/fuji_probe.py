"""Confined Fuji observations using the original catalog and checkpoint Reader."""

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import socket
import re
import time
from urllib.parse import urlsplit
import traceback
import urllib.error
import urllib.request

from conix.catalog import prepare_catalog
from conix.reader import open_reader
from history_observations import snapshot
from fuji_rpc import READ_METHODS

SOURCE = json.loads((Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json").read_text())
ZERO = "0x" + "00" * 20


class CheckFailed(ValueError):
    """A source-controlled acceptance check failed without exposing its inputs."""

    def __init__(self, check):
        super().__init__(SOURCE["checks"][check])
        self.check = check


class ReadRejected(RuntimeError):
    """A bounded provider category without its potentially private explanation."""

    def __init__(self, method, kind, code, *, upstream=None):
        super().__init__("Selected Fuji read was rejected.")
        self.method = method if method in READ_METHODS else "eth_unknown"
        self.kind = kind if kind in ("HTTP", "RPC") else "RPC"
        self.code = code if type(code) is int and abs(code) < 2**31 else "unavailable"
        self.upstream = upstream


def rpc(method, params):
    """Read only through the test gateway; do not print its response prose."""
    request = urllib.request.Request("http://gateway:8080", data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        upstream = None
        try:
            document = json.loads(error.read(4096))
            cause = document.get("cause")
            status = document.get("status")
            if document.get("origin") == "upstream" and type(cause) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", cause):
                upstream = cause
                if type(status) is int and 100 <= status <= 599:
                    upstream += " status " + str(status)
        except (ValueError, TypeError, AttributeError):
            pass  # Unadmitted gateway content never contributes to diagnostics.
        raise ReadRejected(method, "HTTP", error.code, upstream=upstream) from error
    if len(raw) > 2 * 1024 * 1024:
        raise CheckFailed("reply_size")
    result = json.loads(raw)
    if type(result) is dict and "error" in result:
        failure = result["error"]
        raise ReadRejected(method, "RPC", failure.get("code") if type(failure) is dict else None)
    if type(result) is not dict or "result" not in result:
        raise CheckFailed("reply_result")
    return result["result"]


def position_record(fields, values):
    """Present the pinned ABI tuple as named facts without interpreting money."""
    return {
        field["name"]: value.lower() if field["type"] == "address"
        else list(value) if field["type"] == "string[]" else value
        for field, value in zip(fields, values, strict=True)
    }


def direct_upstream_denied():
    """Missing DNS is unavailable evidence; test every resolved TCP destination."""
    upstream = urlsplit(SOURCE["rpc_url"])
    try:
        addresses = socket.getaddrinfo(upstream.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise CheckFailed("upstream_dns") from error
    if not addresses or len(addresses) > 8:
        raise CheckFailed("upstream_dns")
    deadline = time.monotonic() + 8
    for family, kind, protocol, _name, address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CheckFailed("network_deadline")
        with socket.socket(family, kind, protocol) as direct:
            direct.settimeout(min(1, remaining))
            try:
                direct.connect(address)
            except OSError:
                continue
        raise CheckFailed("direct_egress")
    return {"gateway_read": "passed", "upstream_dns": "resolved", "direct_upstream_tcp": "blocked"}


async def reader_observation(request):
    """All state and event reads share the original Reader's exact checkpoint."""
    catalog = prepare_catalog(Path("/run/conix/config.toml"))
    expected_abi = SOURCE["abi_sha256"]
    if hashlib.sha256(Path("/run/conix/abi/pay_to_play.json").read_bytes()).hexdigest() != expected_abi:
        raise CheckFailed("abi")
    async with open_reader(catalog=catalog, database_dsn=request["dsn"], rpc_url=catalog.chain.rpc_url) as reader:
        checkpoint = await reader.checkpoint()
        if checkpoint is None:
            raise CheckFailed("checkpoint")
        events = []
        for descriptor in catalog.contract("payment").descriptor.events:
            page = await reader.events(checkpoint=checkpoint, contract="payment", event=descriptor.signature)
            if not page.complete or page.next_cursor is not None:
                raise CheckFailed("event_page")
            for event in page.events:
                events.append({"address": event.address.lower(), "topics": ["0x" + v.hex() for v in event.topics],
                    "data": "0x" + event.data.hex(), "blockNumber": hex(event.block.number),
                    "blockHash": "0x" + event.block.hash.hex(), "transactionHash": "0x" + event.transaction_hash.hex(),
                    "transactionIndex": hex(event.transaction_index), "logIndex": hex(event.log_index), "removed": False})
        asset = await reader.call(checkpoint=checkpoint, contract="payment", function="asset()", arguments=())
        position = await reader.call(checkpoint=checkpoint, contract="payment",
            function="viewPosition(address,uint256,address)", arguments=(ZERO, 0, ZERO))
        abi = json.loads(Path("/run/conix/abi/pay_to_play.json").read_text())
        fields = next(e for e in abi if e.get("name") == "viewPosition")["outputs"][0]["components"]
        values = position_record(fields, position[0])
        return {"height": checkpoint.block.number, "block_hash": "0x" + checkpoint.block.hash.hex(),
                "source_id": checkpoint.source_id.hex(), "generation": str(checkpoint.generation),
                "events": sorted(events, key=lambda e: (int(e["blockNumber"], 16), int(e["logIndex"], 16))),
                "asset": asset[0].lower(), "position": values}


def main():
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise CheckFailed("input_size")
        request = json.loads(raw)
        operation = request["operation"]
        if operation == "head":
            block = rpc("eth_getBlockByNumber", ["latest", False])
            result = {"height": int(block["number"], 16), "hash": block["hash"]}
        elif operation == "snapshot":
            result = snapshot(request["dsn"])
        elif operation == "reader":
            result = asyncio.run(reader_observation(request))
        elif operation == "source":
            creation = SOURCE["initial_interval"]
            if int(rpc("eth_chainId", []), 16) != SOURCE["chain_id"]:
                raise CheckFailed("chain")
            if rpc("eth_getBlockByNumber", ["0x0", False])["hash"] != SOURCE["genesis_hash"]:
                raise CheckFailed("genesis")
            receipt = rpc("eth_getTransactionReceipt", [SOURCE["receipt"]["transactionHash"]])
            if any(receipt[key] != value for key, value in SOURCE["receipt"].items()):
                raise CheckFailed("receipt")
            logs = [record for record in receipt["logs"] if record["address"].lower() == SOURCE["address"]]
            if [{key: log[key] for key in SOURCE["expected_raw_logs"][0]} for log in logs] != SOURCE["expected_raw_logs"]:
                raise CheckFailed("creation_events")
            code = rpc("eth_getCode", [SOURCE["address"], {"blockHash": creation["block_hash"], "requireCanonical": True}])
            if hashlib.sha256(bytes.fromhex(code[2:])).hexdigest() != SOURCE["runtime_code_sha256"]:
                raise CheckFailed("bytecode")
            result = {"chain_id": SOURCE["chain_id"], "creation_receipt": "matched", "runtime_code": "matched"}
        elif operation == "network-policy":
            # Same internal bridges as the installed process: the gateway
            # must work while a direct TCP connection to its upstream fails.
            if int(rpc("eth_chainId", []), 16) != SOURCE["chain_id"]:
                raise CheckFailed("gateway_control")
            result = direct_upstream_denied()
        elif operation == "deny-write":
            attempted = urllib.request.Request("http://gateway:8080", data=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [],
            }).encode(), headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(attempted, timeout=5).close()
            except urllib.error.HTTPError as error:
                if error.code != 403:
                    raise CheckFailed("write_denial") from None
            else:
                raise CheckFailed("write_admitted")
            result = {"transaction_method": "denied"}
        else:
            raise CheckFailed("operation")
        print(json.dumps(result), flush=True)
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        detail = ""
        if isinstance(error, ReadRejected):
            detail = f" {error.kind} {error.method} code {error.code}."
            if error.upstream is not None:
                detail += " Upstream " + error.upstream + "."
        elif isinstance(error, CheckFailed):
            detail = " Check " + error.check + "."
        raise SystemExit(f"Fuji probe failed ({type(error).__name__}) at {Path(frame.filename).name}:{frame.lineno}." + detail) from None


if __name__ == "__main__":
    main()
