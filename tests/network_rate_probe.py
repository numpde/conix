"""Bounded, read-only population sampling; never an indexing completeness proof.

Only reviewed public endpoints are contacted. Current and historical windows
are exact height positions, not inferred named protocol eras. No retries,
provider fallback, signing inputs, or repository writes are available here.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import traceback

import httpx

from conix.core import _block
from conix.process_diagnostic import emit_unexpected

SOURCE_PATH = Path(__file__).parent / "fixtures/evm/network_rate_sources.json"


class ObservationUnavailable(Exception):
    """Bounded failure metadata without provider prose or endpoint values."""

    def __init__(self, cause, *, code=None):
        super().__init__("Requested network-rate observation is unavailable.")
        self.cause = cause
        self.code = code


def failure_record(error):
    """Keep actual safe cause/location and optional numeric protocol evidence."""
    if isinstance(error, ObservationUnavailable):
        return {"cause": error.cause, **({"code": error.code} if error.code is not None else {})}
    name = type(error).__name__
    name = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "UnexpectedError"
    frames = traceback.extract_tb(error.__traceback__)
    location = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "unavailable"
    if not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]{0,63}\.py:[1-9][0-9]{0,5}|unavailable)", location):
        location = "unavailable"
    return {"cause": name, "location": location}


class Probe:
    def __init__(self, client, endpoint, policy):
        self.client = client
        self.endpoint = endpoint
        self.policy = policy
        self.deadline = time.monotonic() + policy["network_budget_seconds"]
        self.sequence = 0

    def call(self, method, params):
        if time.monotonic() >= self.deadline:
            raise ObservationUnavailable("network_budget")
        self.sequence += 1
        started = time.monotonic()
        with self.client.stream("POST", self.endpoint, json={
            "jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params,
        }, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise ObservationUnavailable("http_status", code=response.status_code)
            raw = bytearray()
            for chunk in response.iter_raw():
                if time.monotonic() >= self.deadline:
                    raise ObservationUnavailable("network_budget")
                raw.extend(chunk)
                if len(raw) > self.policy["response_limit_bytes"]:
                    raise ObservationUnavailable("response_limit")
        timing = {"elapsed_ms": round((time.monotonic() - started) * 1000, 3), "response_bytes": len(raw)}
        document = json.loads(raw)
        if (type(document) is not dict or document.get("jsonrpc") != "2.0"
                or type(document.get("id")) is not int or document["id"] != self.sequence
                or ("result" in document) == ("error" in document)):
            raise ObservationUnavailable("reply_envelope")
        if "error" in document:
            code = document["error"].get("code") if type(document["error"]) is dict else None
            raise ObservationUnavailable("rpc_rejection", code=code if type(code) is int and abs(code) < 2**31 else None)
        return document["result"], timing

    def header(self, height):
        raw, timing = self.call("eth_getBlockByNumber", ["latest" if height is None else hex(height), False])
        if raw is None:
            raise ObservationUnavailable("missing_header")
        expected = int(raw["number"], 16) if height is None else height
        observed = _block(raw, expected)
        record = {"height": observed.header.number, "hash": "0x" + observed.header.hash.hex(),
                  "parent_hash": "0x" + observed.header.parent_hash.hex(), "timestamp": observed.header.timestamp,
                  "transactions": len(observed.transactions), **timing}
        for field in ("timestampMs", "timestampMilliseconds"):
            if field in raw:
                value = raw[field]
                milliseconds = int(value, 16) if type(value) is str and value.startswith("0x") else value
                if type(milliseconds) is not int or milliseconds // 1000 != record["timestamp"]:
                    raise ObservationUnavailable("timestamp_precision")
                if "timestamp_ms" in record and record["timestamp_ms"] != milliseconds:
                    raise ObservationUnavailable("timestamp_precision")
                record.update(timestamp_ms=milliseconds, timestamp_field=field)
        return record


def sample_window(probe, end, label, policy, selector):
    """Separate wide-span means from a small contiguous interarrival sample."""
    start = max(0, end - policy["window_span_blocks"])
    first = probe.header(start)
    last = probe.header(end)
    tail = []
    for height in range(max(start, end - policy["contiguous_tail_blocks"] + 1), end + 1):
        record = last if height == end else first if height == start else probe.header(height)
        if tail and (record["parent_hash"] != tail[-1]["hash"] or record["timestamp"] < tail[-1]["timestamp"]):
            raise ObservationUnavailable("inconsistent_tail")
        if tail and "timestamp_ms" in record and "timestamp_ms" in tail[-1] and record["timestamp_field"] == tail[-1]["timestamp_field"]:
            if record["timestamp_ms"] < tail[-1]["timestamp_ms"]:
                raise ObservationUnavailable("inconsistent_tail")
        tail.append(record)
    field = "timestamp_ms" if "timestamp_ms" in first and "timestamp_ms" in last and first["timestamp_field"] == last["timestamp_field"] else "timestamp"
    duration_ms = (last[field] - first[field]) * (1000 if field == "timestamp" else 1)
    if end <= start or duration_ms <= 0:
        raise ObservationUnavailable("unresolved_cadence")
    filtered = {"blockHash": last["hash"], "address": selector["address"], "topics": [selector["topic0"]]}
    try:
        logs, timing = probe.call("eth_getLogs", [filtered])
        if type(logs) is not list:
            raise ObservationUnavailable("log_array")
        for log in logs:
            if (type(log) is not dict or log.get("blockHash") != last["hash"]
                    or log.get("address", "").lower() != selector["address"]
                    or not log.get("topics") or log["topics"][0] != selector["topic0"]):
                raise ObservationUnavailable("log_selector")
        selected = {"status": "observed", "log_count": len(logs), "filter": filtered, **timing}
    except Exception as error:
        selected = {"status": "unavailable", "filter": filtered, "failure": failure_record(error)}
    recheck = probe.header(end)
    anchor_fields = ("height", "hash", "parent_hash", "timestamp", "transactions", "timestamp_ms", "timestamp_field")
    if tuple(recheck.get(key) for key in anchor_fields) != tuple(last.get(key) for key in anchor_fields):
        raise ObservationUnavailable("changed_window_anchor")
    return {"id": label, "status": "observed", "first": first, "last": last, "tail": tail,
            "height_span": end - start, "duration_ms": duration_ms,
            "timestamp_resolution_ms": 1000 if field == "timestamp" else 1,
            "mean_numbered_block_interval_ms": duration_ms / (end - start), "selector_probe": selected}


def sample_network(network, source, *, transport=None):
    record = {"id": network["id"], "chain_id": network["chain_id"], "endpoint": network["rpc_url"], "phase": "selection", "identity_assurance": "not_checked", "windows": []}
    if network["retired"] or network["rpc_url"] is None:
        return {**record, "status": "retired" if network["retired"] else "no_public_endpoint"}
    try:
        policy = source["sampling"]
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=policy["request_timeout_seconds"], transport=transport) as client:
            probe = Probe(client, network["rpc_url"], policy)
            record["phase"] = "identity"
            identity, _timing = probe.call("eth_chainId", [])
            if type(identity) is not str or int(identity, 16) != network["chain_id"]:
                raise ObservationUnavailable("wrong_chain")
            record["identity_assurance"] = "chain_id_checked"
            record["phase"] = "genesis"
            try:
                record["genesis"] = {"status": "observed", "header": probe.header(0)}
                record["identity_assurance"] = "chain_id_checked_genesis_observed"
            except Exception as error:
                record["genesis"] = {"status": "unavailable", "failure": failure_record(error)}
            record["phase"] = "head"
            record["head"] = probe.header(None)
            record["phase"] = "windows"
            for position in policy["height_positions_percent"]:
                label = "head" if position == 100 else f"height_{position}_percent"
                try:
                    end = record["head"]["height"] * position // 100
                    window = sample_window(probe, end, label, policy, source["selector_probe"])
                except Exception as error:
                    window = {"id": label, "status": "unavailable", "failure": failure_record(error)}
                record["windows"].append(window)
        record["phase"] = "completed"
        record["status"] = "observed" if record["genesis"]["status"] == "observed" and all(w["status"] == "observed" for w in record["windows"]) else "partial"
    except Exception as error:
        record.update(status="unavailable", failure=failure_record(error))
    return record


def main():
    try:
        raw = SOURCE_PATH.read_bytes()
        source = json.loads(raw)
        started = datetime.now(timezone.utc).isoformat()
        with ThreadPoolExecutor(max_workers=source["sampling"]["workers"]) as workers:
            networks = list(workers.map(lambda network: sample_network(network, source), source["networks"]))
        result = {"kind": "observed_network_rates", "source_sha256": hashlib.sha256(raw).hexdigest(),
                  "started_at_utc": started, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                  "scope": "Provider-reported numbered EVM blocks and sampled empty-selector costs; not contract event rates, chain-wide TPS, or finality proof",
                  "networks": networks}
        print(json.dumps(result, separators=(",", ":")), flush=True)
    except Exception as error:
        emit_unexpected(phase="network_measure", error=error, effect="unconfirmed")
        raise SystemExit("Network-rate collection failed; no complete population receipt was produced.") from None


if __name__ == "__main__":
    main()
