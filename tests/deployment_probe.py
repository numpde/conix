"""Contained observations for the retained-deployment acceptance lane.

Only this test producer mines blocks. SQL observations use the supplied runtime
credential in a read-only transaction; the host never imports application code.
Requests and credentials arrive on stdin, never in Docker argv or environment.
"""

import json
from pathlib import Path
import sys
import time
import traceback
import urllib.request


def rpc(method, params):
    """Call the explicitly provisioned fixture without forwarding error prose."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        "http://anvil:8545", data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        document = json.loads(response.read(1048577))
    if type(document) is not dict or "error" in document or "result" not in document:
        raise ValueError("The test RPC did not return an admitted result.")
    return document["result"]


def produce(empty_blocks):
    """Emit an independent ERC-20 Transfer vector after zero to three empty blocks."""
    if type(empty_blocks) is not int or not 0 <= empty_blocks <= 3:
        raise ValueError("The fixture supports at most three empty blocks per observation.")
    for _ in range(empty_blocks):
        rpc("evm_mine", [])
    topic = bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
    sender = bytes.fromhex("00" * 12 + "22" * 20)
    receiver = bytes.fromhex("00" * 12 + "33" * 20)
    runtime = (b"\x60\x07\x60\x00\x52" + b"\x7f" + receiver + b"\x7f" + sender
               + b"\x7f" + topic + b"\x60\x20\x60\x00\xa3\x00")
    token = "0x" + "11" * 20
    rpc("anvil_setCode", [token, "0x" + runtime.hex()])
    account = rpc("eth_accounts", [])[0]
    transaction = rpc("eth_sendTransaction", [{"from": account, "to": token, "gas": "0x100000"}])
    # Submission does not establish mining. Observe the same transaction under
    # one deadline; never submit another transaction to conceal an unknown result.
    deadline = time.monotonic() + 5
    while (receipt := rpc("eth_getTransactionReceipt", [transaction])) is None:
        if time.monotonic() >= deadline:
            raise TimeoutError("The fixture transaction outcome remains unknown.")
        time.sleep(0.02)
    if receipt["status"] != "0x1" or len(receipt["logs"]) != 1:
        raise ValueError("The fixture Transfer was not confirmed.")
    return {"height": int(receipt["blockNumber"], 16), "transaction": transaction}


def snapshot(dsn):
    """Observe durable history without granting this helper schema-owner authority."""
    import psycopg

    with psycopg.connect(dsn.strip(), connect_timeout=5, options="-c default_transaction_read_only=on") as db:
        row = db.execute(
            "SELECT last_block,revision,generation::text,encode(source_id,'hex'),blocked_reason "
            "FROM conix.source_progress WHERE singleton=1"
        ).fetchone()
        blocks = db.execute(
            "SELECT number,encode(hash,'hex'),encode(parent_hash,'hex') FROM conix.blocks ORDER BY number"
        ).fetchall()
        events = db.execute(
            "SELECT block_number,encode(transaction_hash,'hex'),log_index "
            "FROM conix.events ORDER BY block_number,log_index"
        ).fetchall()
    return {"height": row[0], "revision": row[1], "generation": row[2],
            "source_id": row[3], "blocked": row[4], "blocks": blocks, "events": events}


def main():
    """Return one bounded test observation; expose failure type and source only."""
    try:
        payload = sys.stdin.buffer.read(8193)
        if len(payload) > 8192:
            raise ValueError("Deployment probe request exceeds its limit.")
        request = json.loads(payload)
        if request["operation"] == "genesis":
            result = {"hash": rpc("eth_getBlockByNumber", ["0x0", False])["hash"]}
        elif request["operation"] == "produce":
            result = produce(request["empty_blocks"])
        elif request["operation"] == "snapshot":
            result = snapshot(request["dsn"])
        else:
            raise ValueError("Unknown deployment probe operation.")
        print(json.dumps(result), flush=True)
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        raise SystemExit(
            f"Deployment probe failed ({type(error).__name__}) "
            f"at {Path(frame.filename).name}:{frame.lineno}."
        ) from None


if __name__ == "__main__":
    main()
