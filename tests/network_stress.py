"""Exercise the original acquisition path with an explicit synthetic cost model.

The sink records publication arguments; it does not represent PostgreSQL.
Model milliseconds do not replace the real transport's deadline clock.
"""

from collections import Counter
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import tempfile
import time

import httpx

from conix.catalog import prepare_catalog
from conix.core import Writer
from conix.rpc import RPC
from test_core_rpc import RecordingJournal, json_response

FIXTURES = Path(__file__).parent / "fixtures"
DEPLOYED = json.loads((FIXTURES / "evm/fuji_pay_to_play.json").read_text())
ABI = FIXTURES / "abi/fuji_pay_to_play.json"


def catalog_for_workload(workload, directory):
    """Use the deployed generic ABI; selector count is a workload dimension."""
    abi = json.loads(ABI.read_bytes())
    signatures = [entry["name"] + "(" + ",".join(field["type"] for field in entry["inputs"]) + ")"
                  for entry in abi if entry.get("type") == "event"]
    ownership = "OwnershipTransferred(address,address)"
    selected = [ownership] if workload["selectors"] == 1 else signatures
    if len(selected) != workload["selectors"]:
        raise ValueError("Stress workload selector count has no reviewed ABI selection.")
    (directory / "payment.json").write_bytes(ABI.read_bytes())
    source = f'''version = 1
[chain]
chain_id = 31337
genesis_hash = "0x{'00' * 32}"
start_block = 1
rpc_url = "http://stand-in.invalid/"
[contracts.payment]
address = "{DEPLOYED['address']}"
abi_file = "payment.json"
events = {json.dumps(selected)}
[database]
dsn_file = "/run/conix/connection.dsn"
runtime_role = "conix_runtime"
'''
    # Share the reviewed contained limits; this test grants no larger budget.
    source += "[limits]\n" + (FIXTURES / "catalog.toml").read_text().split("[limits]\n", 1)[1]
    (directory / "catalog.toml").write_text(source)
    return prepare_catalog(directory / "catalog.toml")


class StandIn:
    """Supply consistent raw wire evidence while accounting for serial RPC cost."""

    def __init__(self, workload, catalog):
        self.workload = workload
        self.catalog = catalog
        self.model_ms = 0
        self.requests = Counter()
        self.headers = {}
        self.selectors = {"0x" + event.topic0.hex() for event in catalog.contract("payment").descriptor.events}

    @staticmethod
    def block_hash(height):
        return "0x" + (bytes(32) if height == 0 else hashlib.sha256(f"stress-{height}".encode()).digest()).hex()

    def block(self, height):
        count = max(self.workload["transactions_per_block"], bool(self.event_count(height)))
        transactions = ["0x" + hashlib.sha256(f"tx-{height}-{index}".encode()).hexdigest() for index in range(count)]
        record = {"number": hex(height), "hash": self.block_hash(height),
                  "parentHash": self.block_hash(max(0, height - 1)),
                  "timestamp": hex(1700000000 + int(height * Fraction(str(self.workload["block_interval_ms"])) // 1000)),
                  "transactions": transactions}
        self.headers[record["hash"]] = record
        return record

    def event_count(self, height):
        return self.workload["events_per_emitting_block"] if height and height % self.workload["emit_every_n_blocks"] == 0 else 0

    def __call__(self, request):
        operation = json.loads(request.content)
        method, params = operation["method"], operation["params"]
        if method == "eth_chainId":
            result = hex(self.catalog.chain.chain_id)
        elif method == "eth_getBlockByNumber":
            self.model_ms += self.workload["header_ms"]
            head = self.workload["initial_backlog"] + int(Fraction(str(self.model_ms)) // Fraction(str(self.workload["block_interval_ms"])))
            height = head if params[0] == "latest" else int(params[0], 16)
            result = self.block(height) if height <= head else None
        elif method == "eth_getLogs":
            self.model_ms += self.workload["log_ms"]
            (selector,) = params
            assert set(selector) == {"blockHash", "address", "topics"}
            assert selector["address"] == DEPLOYED["address"] and selector["topics"][0] in self.selectors
            header = self.headers[selector["blockHash"]]
            self.requests[(selector["blockHash"], selector["topics"][0])] += 1
            prototype = DEPLOYED["expected_raw_logs"][0]
            count = self.event_count(int(header["number"], 16)) if selector["topics"] == prototype["topics"][:1] else 0
            result = [{**prototype, "blockNumber": header["number"], "blockHash": header["hash"],
                       "transactionHash": header["transactions"][0], "transactionIndex": "0x0", "logIndex": hex(index)}
                      for index in range(count)]
        else:
            raise AssertionError("Stand-in received an operation outside the original acquisition path.")
        return json_response({"jsonrpc": "2.0", "id": operation["id"], "result": result})


async def replay(workload):
    """Return recorded evidence and model metrics, never a database throughput claim."""
    with tempfile.TemporaryDirectory() as temporary:
        catalog = catalog_for_workload(workload, Path(temporary))
    stand_in = StandIn(workload, catalog)
    sink = RecordingJournal(catalog)
    async with httpx.AsyncClient(transport=httpx.MockTransport(stand_in), trust_env=False, timeout=None) as client:
        writer = Writer(catalog=catalog, journal=sink, rpc=RPC(client=client, url=catalog.chain.rpc_url, limits=catalog.limits))
        await writer.verify_source()
        stand_in.model_ms = 0  # Admission is setup, separate from steady acquisition.
        started = time.monotonic()
        for _ in range(workload["blocks"]):
            await writer.advance_one()
        actual_seconds = time.monotonic() - started
    per_block_ms = 3 * workload["header_ms"] + workload["selectors"] * workload["log_ms"]
    if max(workload["header_ms"], workload["log_ms"]) > catalog.limits.rpc_timeout_seconds * 1000 or per_block_ms > catalog.limits.operation_timeout_seconds * 1000:
        outcome = "rpc_deadline_exceeded"
    else:
        outcome = "rpc_headroom" if per_block_ms < workload["block_interval_ms"] else "rpc_bottleneck"
    final_head = workload["initial_backlog"] + int(Fraction(str(stand_in.model_ms)) // Fraction(str(workload["block_interval_ms"])))
    return {"model": outcome, "initial_backlog": workload["initial_backlog"],
            "final_backlog": final_head - sink.basis.last_block.number, "modeled_steady_rpc_ms_per_block": per_block_ms,
            "modeled_elapsed_ms": stand_in.model_ms, "local_scanner_seconds": actual_seconds,
            "demand_blocks_per_second": 1000 / workload["block_interval_ms"],
            "capacity_upper_bound_blocks_per_second": None if outcome == "rpc_deadline_exceeded" else 1000 / per_block_ms}, sink, stand_in
