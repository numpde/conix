"""Read-only checkpoint sampler and original Reader verifier for Stage C.

The periodic observation reads only PostgreSQL's committed progress row.
The original Reader validates the final raw-event multiset and checkpoint;
no test-only scanner, decoder or publication sink is involved.
"""
import argparse
import asyncio
from collections import Counter
import json
import os
import sys
from pathlib import Path
import time
from urllib.request import urlopen

import psycopg

from conix.catalog import prepare_catalog
from conix.reader import open_reader
from conix.process_diagnostic import emit_unexpected
from stress_replay import ReplayEvidence, require, selected_event

EVIDENCE = Path("/evidence")
RESULTS = Path("/results")
CATALOG = Path("/run/conix/config.toml")
CREDENTIAL = Path("/run/conix/connection.dsn")
STATUS = "http://replay:8080/status"


def service():
    # The single-threaded replay service serializes control reads with an
    # explicitly delayed RPC. A legal 5-second slowdown must not be mistaken
    # for a failed health observation while it is serving that read.
    with urlopen(STATUS, timeout=15) as response:
        raw = response.read(16385)
    require(0 < len(raw) <= 16384, "Replay status response exceeded its bound.")
    result = json.loads(raw)
    require(type(result) is dict and type(result.get("released")) is bool
            and type(result.get("head")) is int and type(result.get("elapsed_ns")) is int
            and type(result.get("frozen")) is bool, "Replay status is not admitted.")
    return result


def progress(dsn):
    """A single immutable, read-only PostgreSQL row in one short transaction."""
    with psycopg.connect(
        dsn, connect_timeout=5, options="-c default_transaction_read_only=on",
    ) as db:
        row = db.execute(
            "SELECT last_block, revision, generation::text, encode(source_id,'hex'), blocked_reason "
            "FROM conix.source_progress WHERE singleton=1"
        ).fetchone()
    require(row is not None and (row[0] is None or type(row[0]) is int),
            "Durable Conix progress row is absent.")
    return {"height": row[0], "revision": row[1], "generation": row[2],
            "source_id": row[3], "blocked": row[4]}


async def verify_reader(evidence, catalog, dsn):
    """Use original Reader pages, retaining duplicates and the exact raw bytes."""
    observed = Counter()
    async with open_reader(catalog=catalog, database_dsn=dsn,
                           rpc_url=catalog.chain.rpc_url) as reader:
        checkpoint = await reader.checkpoint()
        require(checkpoint is not None and checkpoint.block.number == evidence.final
                and checkpoint.block.hash.hex() == evidence.headers[evidence.final]["hash"][2:].lower()
                and checkpoint.source_id == evidence.catalog.source_id
                and checkpoint.history_start == evidence.start,
                "Original Reader did not observe the admitted complete checkpoint.")
        for contract in catalog.contracts:
            for event in contract.descriptor.events:
                cursor = None
                while True:
                    page = await reader.events(
                        checkpoint=checkpoint, contract=contract.alias,
                        event=event.signature, cursor=cursor,
                        limit=min(100, catalog.limits.max_page_size),
                    )
                    for row in page.events:
                        observed[(
                            row.address.lower(), "0x" + row.block.hash.hex(),
                            row.block.number, "0x" + row.transaction_hash.hex(),
                            row.transaction_index, row.log_index,
                            tuple("0x" + topic.hex() for topic in row.topics),
                            "0x" + row.data.hex(),
                        )] += 1
                    if page.complete:
                        require(page.next_cursor is None,
                                "Reader page has a cursor after completion.")
                        break
                    require(page.next_cursor is not None and page.next_cursor != cursor
                            and page.events, "Original Reader pagination stalled.")
                    cursor = page.next_cursor
        require(observed == evidence.expected, "Original Reader raw events differ from admitted matrix.")
        return {"height": checkpoint.block.number,
                "hash": "0x" + checkpoint.block.hash.hex(),
                "source_id": checkpoint.source_id.hex(),
                "generation": str(checkpoint.generation),
                "events": sum(observed.values()), "distinct_raw_events": len(observed)}


async def verify_before_deadline(evidence, catalog, dsn, *, remaining_seconds):
    """The final Reader proof shares the catch-up grant with publication."""
    if remaining_seconds <= 0:
        raise TimeoutError("Reader verification has no remaining catch-up time.")
    deadline = time.monotonic() + remaining_seconds
    async with asyncio.timeout(remaining_seconds):
        verified = await verify_reader(evidence, catalog, dsn)
    if time.monotonic() >= deadline:
        raise TimeoutError("Reader verification exceeded the catch-up deadline.")
    return verified


def row(state, checkpoint, start):
    head = state["head"]
    height = checkpoint["height"]
    require(height is None or start <= height <= head,
            "Durable checkpoint exceeds the currently visible head.")
    return {
        "elapsed_ns": state["elapsed_ns"], "head": head,
        "checkpoint": height, "checkpoint_lag": (
            None if height is None else max(0, head - height)),
        "pending_blocks": max(0, head - start + 1) if height is None else None,
        "checkpoint_revision": checkpoint["revision"],
    }


def emit(path, document):
    """Atomic owner-only result; failure evidence is not a successful report."""
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    require(0 < len(raw) <= 2 * 1048576, "Replay sample report exceeds its limit.")
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def observe(*, cadence_ms, catchup_seconds, start_wait_seconds):
    evidence = ReplayEvidence(EVIDENCE)
    catalog = prepare_catalog(CATALOG)
    require(catalog.source_id == evidence.catalog.source_id
            and catalog.chain.rpc_url == "http://replay:8080"
            and catalog.chain.start_block == evidence.start,
            "Selected Reader source differs from the admitted replay workload.")
    dsn = CREDENTIAL.read_text().strip()
    require(0 < len(dsn) <= 8192, "Reader-only credential is unavailable.")
    result = {
        "status": "unavailable", "start": evidence.start, "final": evidence.final,
        "source_id": catalog.source_id.hex(),
        "delay_policy": "server-configured fixed per admitted RPC response",
        "window_ns": evidence.window_ns, "sample_cadence_ms": cadence_ms,
        "catchup_timeout_seconds": catchup_seconds,
        "window": [], "catchup": [], "first_checkpoint_ns": None,
        "reader": None, "failure": None,
    }
    report = RESULTS / "result.json"
    primary = None
    try:
        ready = service()
        require(not ready["released"] and ready["elapsed_ns"] == 0
                and ready["head"] == evidence.timeline[0][1],
                "Replay head advanced before the original Reader sampler was ready.")
        emit(RESULTS / "ready.json", {
            "status": "ready", "source_id": evidence.catalog.source_id.hex(),
            "initial_head": ready["head"], "cadence_ms": cadence_ms,
        })
        deadline = time.monotonic() + start_wait_seconds
        while True:
            state = service()
            if state["released"]:
                break
            require(state["head"] == evidence.timeline[0][1]
                    and state["elapsed_ns"] == 0,
                    "Replay head advanced before the common release.")
            require(time.monotonic() < deadline, "Sampler waited past release deadline.")
            time.sleep(0.1)
        next_sample = time.monotonic()
        window_end = evidence.window_ns
        final_deadline = window_end + catchup_seconds * 1_000_000_000
        while True:
            before = service()
            checkpoint = progress(dsn)
            status_requested_at = time.monotonic()
            state = service()
            require(checkpoint["source_id"] == catalog.source_id.hex()
                    and checkpoint["blocked"] is None,
                    "Durable source changed or was blocked during replay.")
            measured = row(state, checkpoint, evidence.start)
            measured["head_before"] = before["head"]
            measured["observation_span_ns"] = state["elapsed_ns"] - before["elapsed_ns"]
            if state["elapsed_ns"] <= window_end:
                result["window"].append(measured)
            else:
                result["catchup"].append(measured)
            if checkpoint["height"] is not None and result["first_checkpoint_ns"] is None:
                result["first_checkpoint_ns"] = state["elapsed_ns"]
            if state["elapsed_ns"] >= final_deadline:
                result["status"] = "incomplete_catchup"
                result["failure"] = "catchup_deadline"
                break
            if state["frozen"] and checkpoint["height"] == evidence.final:
                # Charge the whole control roundtrip conservatively. No
                # assumption about shared container monotonic clocks is needed.
                remaining = ((final_deadline - state["elapsed_ns"]) / 1000000000
                             - (time.monotonic() - status_requested_at))
                try:
                    result["reader"] = asyncio.run(verify_before_deadline(
                        evidence, catalog, dsn, remaining_seconds=remaining))
                except TimeoutError:
                    result["status"] = "incomplete_catchup"
                    result["failure"] = "catchup_deadline"
                else:
                    result["status"] = "complete"
                break
            next_sample += cadence_ms / 1000
            time.sleep(max(0, next_sample - time.monotonic()))
        result["service"] = service()
        require(result["window"], "Replay has no measured live window.")
        require(result["service"]["errors"] == 0,
                "Original client issued unsupported or unadmitted RPC reads.")
    except BaseException as error:
        primary = error
        result["status"] = "unavailable"
        result["failure"] = type(error).__name__
    try:
        emit(report, result)
    except BaseException as error:
        if primary is None:
            primary = error
        else:
            primary.add_note("Replay observation result publication also failed.")
    if primary is not None:
        raise primary


def main():
    # A separate bounded docker-exec control observes the same retained
    # sampler process; readiness never depends on its log output.
    if sys.argv[1:] == ["ready"]:
        raw = (RESULTS / "ready.json").read_bytes()
        require(0 < len(raw) <= 4096, "Sampler readiness is unavailable.")
        record = json.loads(raw)
        require(record.get("status") == "ready"
                and type(record.get("initial_head")) is int
                and type(record.get("source_id")) is str,
                "Sampler readiness has malformed identity.")
        print(json.dumps(record, separators=(",", ":")))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-ms", type=int, required=True)
    parser.add_argument("--catchup-seconds", type=int, required=True)
    parser.add_argument("--start-wait-seconds", type=int, required=True)
    args = parser.parse_args()
    require(100 <= args.sample_ms <= 60000 and 1 <= args.catchup_seconds <= 3600
            and 10 <= args.start_wait_seconds <= 900,
            "Sampler cadence, catch-up and release budgets must be explicitly bounded.")
    observe(cadence_ms=args.sample_ms, catchup_seconds=args.catchup_seconds,
            start_wait_seconds=args.start_wait_seconds)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as error:
        emit_unexpected(phase="reader_check", error=error, effect="unconfirmed")
        raise SystemExit(1) from None
