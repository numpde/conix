"""Complete immutable RPC evidence; never scan, decode events or publish SQL.

The installed catalog and header validators remain authoritative. Retained
observations fix the entire interval; acquisition cannot narrow or restart it.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import tomllib

import httpx

from conix.catalog import prepare_catalog
from conix.core import _block, _quantity, verify_source
from conix.process_diagnostic import emit_unexpected
from conix.rpc import RPC
from http_recording import RecordingTransport
from rpc_gateway import admit_configuration, upstream_addresses
from stress_archive import _decoded, document, read_archive, rpc_key
from stress_observer import admit_window
from stress_protocol import BUNDLE_INPUTS, RECEIPT


def unpack(stream, folder, *, completion=False):
    """Receive only exact bounded regular members; never extract archive paths."""
    allowed = dict(BUNDLE_INPUTS)
    if completion:
        allowed["completion.warc.gz"] = 64 * 1048576
    seen = set()
    with tarfile.open(fileobj=stream, mode="r|") as bundle:
        for member in bundle:
            if not (member.name in allowed and member.name not in seen and member.isfile()
                    and 0 < member.size <= allowed[member.name]):
                raise ValueError("Evidence bundle contains an ungranted member.")
            seen.add(member.name)
            source = bundle.extractfile(member)
            remaining = member.size
            with (folder / member.name).open("xb") as target:
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not (chunk):
                        raise ValueError("Evidence bundle member is incomplete.")
                    target.write(chunk)
                    remaining -= len(chunk)
    if not (seen == allowed.keys()):
        raise ValueError("Evidence bundle omits a required input.")


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _catalog(folder, manifest, source):
    """Check the host projection against the retained declaration, not current config."""
    original = tomllib.loads((folder / "deployment.toml").read_text())
    projected = tomllib.loads((folder / "catalog.toml").read_text())
    if type(original.get("version")) is not int or original["version"] != 1:
        raise ValueError("Retained deployment has an unsupported configuration version.")
    chain = dict(original["chain"], rpc_url=source["rpc_url"])
    if not (chain["chain_id"] == source["chain_id"]
            and chain["genesis_hash"] == source["genesis_hash"]
            and chain["start_block"] == manifest["start_block"]):
        raise ValueError("Retained source identity conflicts with its declaration.")
    if not (len(original["contracts"]) == 1):
        raise ValueError("This capture snapshot requires one retained ABI.")
    contracts = {}
    for (alias, selected) in original["contracts"].items():
        if not (selected["address"].lower() == source["address"].lower()):
            raise ValueError("Retained contract address conflicts with its source.")
        contracts[alias] = dict(selected, abi_file="abi.json")
    expected = {"version": 1, "chain": chain, "contracts": contracts, "limits": original["limits"],
                "database": {"dsn_file": "/run/conix/connection.dsn", "runtime_role": "conix_runtime"}}
    if not (projected == expected):
        raise ValueError("Evidence catalog projection changed retained selections.")
    return prepare_catalog(folder / "catalog.toml")


def _timeline(manifest, observer):
    """Bind every admitted poll to its own archived conversation and clock interval."""
    if not (manifest["capture"] == "complete" and manifest["process_continuity"] == "confirmed"
            and manifest["observation_eligibility"]["status"] == "complete"
            and not manifest["cleanup"]
            and set(manifest["worker_exits"]) == {"gateway", "observer"}
            and all(type(value) is int and value == 0 for value in manifest["worker_exits"].values())):
        raise ValueError("Retained capture does not establish a complete observation window.")
    events = manifest["indexer_events"]["events"]
    if not (len(events) == 1 and events[0]["action"] == "start"
            and type(events[0]["time_ns"]) is int and events[0]["time_ns"] > 0):
        raise ValueError("Retained process lifecycle is incomplete.")
    if not (set(manifest["clocks"]) == {"gateway", "observer"}):
        raise ValueError("Retained capture lacks the required clock mappings.")
    tolerance = manifest["selected_parameters"]["stress_clock_tolerance_ms"] * 1000000
    for clock in manifest["clocks"].values():
        for point in (clock["before"], clock["after"]):
            if not (point["sent"] <= point["received"]
                    and point["lower_offset_ns"] == point["worker"] - point["received"]
                    and point["upper_offset_ns"] == point["worker"] - point["sent"]
                    and point["received"] - point["sent"] <= tolerance):
                raise ValueError("Retained clock calibration is invalid.")
        lower = max(clock["before"]["lower_offset_ns"], clock["after"]["lower_offset_ns"])
        upper = min(clock["before"]["upper_offset_ns"], clock["after"]["upper_offset_ns"])
        if not (lower <= upper and clock["offset_ns"] == (lower + upper) // 2
                and clock["uncertainty_ns"] == (upper - lower) // 2):
            raise ValueError("Retained monotonic clock mapping conflicts.")
    samples = manifest["observer"]["samples"]
    maximum_gap = manifest["selected_parameters"]["stress_max_gap_ms"] * 1000000
    if not (admit_window(samples, maximum_gap)["status"] == "complete"):
        raise ValueError("Retained observation timeline is unavailable.")
    polls = [manifest["seed"], *samples]
    observed_heights = [sample["header"]["height"] for sample in polls if sample["status"] == "observed"]
    if observed_heights != sorted(observed_heights):
        raise ValueError("Retained head observations decrease after the seed.")
    window = manifest["observer"]
    duration = manifest["selected_parameters"]["stress_duration_seconds"]
    if not (window["requested_duration_seconds"] == duration
            and samples[0]["started_ns"] >= window["released_ns"]
            and samples[-1]["started_ns"] >= window["released_ns"] + duration * 1000000000
            and window["actual_span_ns"] == samples[-1]["finished_ns"] - samples[0]["started_ns"]):
        raise ValueError("Retained samples do not establish the declared observation duration.")
    offset = manifest["clocks"]["observer"]["offset_ns"]
    for sample in samples:
        if not (sample["elapsed_start_ns"] == sample["started_ns"] - offset - manifest["origin_ns"]
                and sample["elapsed_end_ns"] == sample["finished_ns"] - offset - manifest["origin_ns"]):
            raise ValueError("Retained poll timeline conflicts with its monotonic clock mapping.")
    if not (manifest["seed"] == manifest["observer"]["seed"] and len(polls) == len(observer)):
        raise ValueError("Retained timeline omits or adds recorded polls.")
    for (sample, exchange) in zip(polls, observer, strict=True):
        if not (type(sample["id"]) is int and sample["status"] in ("observed", "unavailable")
                and type(sample["started_ns"]) is int and type(sample["finished_ns"]) is int
                and exchange.key == rpc_key("eth_getBlockByNumber", ["latest", False])
                and exchange.request["id"] == sample["id"]
                and sample["started_ns"] <= exchange.started_ns <= exchange.finished_ns <= sample["finished_ns"]):
            raise ValueError("Retained poll does not match its archived timing and request.")
        if sample["status"] == "observed":
            header = sample["header"]
            block = _block(exchange.result(), header["height"]).header
            if not (header == {"height": block.number, "hash": "0x" + block.hash.hex(),
                               "parent_hash": "0x" + block.parent_hash.hex()}):
                raise ValueError("Retained poll conflicts with its archived header.")
        else:
            try:
                raw = exchange.result()
                _block(raw, _quantity(raw["number"]))
            except (ValueError, RuntimeError, KeyError, TypeError):
                continue
            raise ValueError("A failed retained poll carries contradictory successful evidence.")
    if not (polls[0]["status"] == "observed" and polls[0]["header"]["height"] == manifest["start_block"]):
        raise ValueError("Retained seed does not establish the configured start height.")
    return polls


def load(folder):
    """Admit A before any network request; its original files stay immutable."""
    hashes = {name: file_hash(folder / name) for name in BUNDLE_INPUTS}
    manifest = document((folder / "manifest.json").read_bytes())
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("Retained capture has an unsupported manifest version.")
    source = document((folder / "source.json").read_bytes())
    for (name, field) in (("source.json", "source_sha256"), ("abi.json", "abi_sha256"),
                        ("deployment.toml", "deployment_sha256")):
        if not (hashes[name] == manifest[field]):
            raise ValueError("Retained source snapshot hash differs from the capture manifest.")
    if not (hashes["abi.json"] == source["abi_sha256"]):
        raise ValueError("Retained ABI differs from its reviewed source.")
    admit_configuration(source["rpc_url"], None)
    streams = {}
    for name in ("application", "observer"):
        exported = manifest["export"][name]
        receipt = manifest["gateway_receipt" if name == "application" else "observer_receipt"]
        if not (exported["status"] == "verified" and exported["sha256"] == hashes[name + ".warc.gz"]
                and receipt["status"] == "complete" and not receipt["transport_stopped"]
                and receipt["cleanup_failures"] == receipt["recording_failures"] == 0):
            raise ValueError("Retained archive is incomplete or differs from its qualified export.")
        streams[name] = read_archive(folder / (name + ".warc.gz"), source["rpc_url"])
        if not (len(streams[name]) == receipt["exchanges"] == exported["inspection"]["exchanges"]):
            raise ValueError("Retained archive count differs from its completion evidence.")
    catalog = _catalog(folder, manifest, source)
    polls = _timeline(manifest, streams["observer"])
    return catalog, manifest, streams["application"], polls, hashes


class EvidenceRPC:
    """Reuse exact immutable reads; every repeat must agree before reuse."""

    def __init__(self, exchanges, fetch, deadline):
        self.exchanges = {}
        for exchange in exchanges:
            self.exchanges.setdefault(exchange.key, []).append(exchange)
        self.fetch = fetch
        self.deadline = deadline
        self.reused = 0
        self.acquired = 0
        self.completed = {}

    async def call(self, method, params, *, deadline):
        if not (time.monotonic() < min(deadline, self.deadline)):
            raise ValueError("Evidence completion deadline expired.")
        key = rpc_key(method, params)
        if key in self.completed:
            return self.completed[key]
        if key in self.exchanges:
            values = [exchange.result() for exchange in self.exchanges[key]]
            encoded = [json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) for value in values]
            if not (len(set(encoded)) == 1):
                raise ValueError("Repeated immutable captured results conflict.")
            self.reused += 1
            return values[0]
        self.acquired += 1
        if not (self.fetch is not None):
            raise ValueError("Required immutable RPC evidence is absent.")
        result = await self.fetch.call(method, params, deadline=min(deadline, self.deadline))
        self.completed[key] = result
        return result


def required_heights(catalog, polls):
    """Bound the full interval before allocating any derived coverage index."""
    start = catalog.chain.start_block
    final = polls[-1]["header"]["height"]
    if not (type(final) is int and start <= final and final - start < 4096):
        raise ValueError("Retained interval exceeds the completion height budget.")
    return sorted({0, *range(max(0, start - 1), final + 1)})


async def complete_matrix(catalog, application, polls, fetch, deadline):
    """Fill the whole seed-through-final matrix, then recheck every numbered block."""
    heights = required_heights(catalog, polls)
    evidence = EvidenceRPC(application, fetch, deadline)
    await verify_source(catalog=catalog, rpc=evidence)
    await verify_source(catalog=catalog, rpc=EvidenceRPC((), fetch, deadline))
    (start, final) = catalog.chain.start_block, polls[-1]["header"]["height"]
    headers = {}
    blocks = {}
    previous = None
    for height in heights:
        raw = await evidence.call("eth_getBlockByNumber", [hex(height), False], deadline=deadline)
        block = _block(raw, height).header
        if previous is not None and height == previous.number + 1:
            if not (block.parent_hash == previous.hash):
                raise ValueError("Required numbered headers do not form a contiguous branch.")
        previous = block
        headers[height] = raw
        blocks[height] = block
    for sample in polls:
        if sample["status"] == "observed":
            observed = sample["header"]
            block = blocks[observed["height"]]
            if not ("0x" + block.hash.hex() == observed["hash"] and "0x" + block.parent_hash.hex() == observed["parent_hash"]):
                raise ValueError("Completed branch conflicts with a retained head observation.")
    selectors = sorted({("0x" + contract.address.hex(), "0x" + event.topic0.hex())
                        for contract in catalog.contracts for event in contract.descriptor.events})
    cells = 0
    for height in range(start, final + 1):
        for (address, topic) in selectors:
            params = [{"blockHash": "0x" + blocks[height].hash.hex(), "address": address, "topics": [topic]}]
            result = await evidence.call("eth_getLogs", params, deadline=deadline)
            if not (type(result) is list):
                raise ValueError("Required selected log evidence is not an array.")
            cells += 1
    for (height, original) in headers.items():
        current = await fetch.call("eth_getBlockByNumber", [hex(height), False], deadline=deadline)
        _block(current, height)
        if not (json.dumps(current, sort_keys=True, allow_nan=False) == json.dumps(original, sort_keys=True, allow_nan=False)):
            raise ValueError("Numbered header changed during completion canonical recheck.")
    return {"start_block": start, "final_block": final, "headers": len(headers),
            "selectors": len(selectors), "log_cells": cells,
            "reused": evidence.reused, "acquired": evidence.acquired, "rechecks": len(headers)}


class RecordedAsyncTransport(httpx.AsyncBaseTransport):
    """Bridge the existing serial recorder to the original asynchronous RPC decoder."""

    def __init__(self, recorder):
        self.recorder = recorder

    def _forward(self, request):
        response = self.recorder.handle_request(request)
        try:
            body = _decoded(b"".join(response.iter_raw()), response.headers.multi_items())
            headers = [(key, value) for (key, value) in response.headers.multi_items()
                       if key.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(response.status_code, stream=httpx.ByteStream(body), headers=headers)
        finally:
            response.close()

    async def handle_async_request(self, request):
        await request.aread()
        pending = asyncio.create_task(asyncio.to_thread(self._forward, request))
        try:
            return await asyncio.shield(pending)
        except BaseException:
            # A request deadline does not retire a synchronous I/O thread.
            # Join its bounded transport before recorder closure or exit.
            try:
                await pending
            except BaseException:
                pass
            raise

    async def aclose(self):
        self.recorder.close()


async def verify_completion(folder, catalog, application, polls):
    """Rebuild coverage from closed WARC bytes in a separate networkless process."""
    completed = read_archive(folder / "completion.warc.gz", catalog.chain.rpc_url, max_exchanges=16384)
    heights = required_heights(catalog, polls)
    if not (len(completed) >= len(heights)):
        raise ValueError("Completion archive lacks its canonical recheck sequence.")
    for (height, exchange) in zip(heights, completed[-len(heights):], strict=True):
        if not (exchange.key == rpc_key("eth_getBlockByNumber", [hex(height), False])):
            raise ValueError("Completion archive does not end with every numbered canonical recheck.")
        _block(exchange.result(), height)
    deadline = time.monotonic() + 60
    recorded = [*application, *completed]
    cache = EvidenceRPC(recorded, None, deadline)
    coverage = await complete_matrix(catalog, recorded, polls, cache, deadline)
    return {"status": "verified", "completion_sha256": file_hash(folder / "completion.warc.gz"),
            "completion_exchanges": len(completed), "coverage": coverage}


class ArchiveOutput:
    """Hash precisely the physical bytes accepted by the attached stdout pipe."""

    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def write(self, data):
        written = self.stream.write(data)
        if type(written) is int and written > 0:
            self.digest.update(data[:written])
        return written

    def flush(self):
        self.stream.flush()


async def collect(catalog, application, polls, *, seconds, exchanges, archive_bytes, output):
    upstream_addresses(upstream_url=catalog.chain.rpc_url)
    transport = RecordingTransport(httpx.HTTPTransport(retries=0, trust_env=False), output,
                                   max_request_bytes=16384, max_response_bytes=2 * 1048576,
                                   elapsed_budget_seconds=seconds, max_exchanges=exchanges,
                                   max_archive_bytes=archive_bytes)
    client = httpx.AsyncClient(transport=RecordedAsyncTransport(transport), trust_env=False,
                              timeout=min(15, catalog.limits.rpc_timeout_seconds))
    rpc = RPC(client=client, url=catalog.chain.rpc_url, limits=catalog.limits)
    primary = None
    result = None
    try:
        result = await complete_matrix(catalog, application, polls, rpc, time.monotonic() + seconds)
    except BaseException as error:
        primary = error
    try:
        await client.aclose()
    except BaseException:
        if primary is None:
            raise
        primary.add_note("Completion recorder retirement also failed; preserve partial evidence.")
    if primary is not None:
        raise primary
    return dict(result, completion_exchanges=transport.exchanges)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("admit", "complete", "verify"))
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--exchanges", type=int, default=4096)
    parser.add_argument("--archive-bytes", type=int, default=64 * 1048576)
    options = parser.parse_args()
    phase = "input_admission"
    receipt = {"status": "unavailable", "phase": phase}
    try:
        if not (1 <= options.seconds <= 1800 and 1 <= options.exchanges <= 16384
                and 1048576 <= options.archive_bytes <= 64 * 1048576):
            raise ValueError("Completion budgets exceed the permitted grant.")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            unpack(sys.stdin.buffer, folder, completion=options.operation == "verify")
            (catalog, manifest, application, polls, hashes) = load(folder)
            receipt.update({"input_sha256": hashes, "source_id": catalog.source_id.hex(),
                            "capture_revision": manifest["revision"]})
            if options.operation == "admit":
                print(json.dumps(dict(receipt, status="eligible"), separators=(",", ":")))
                return
            if options.operation == "verify":
                phase = "archive_verification"
                verified = asyncio.run(verify_completion(folder, catalog, application, polls))
                print(json.dumps(verified, separators=(",", ":")))
                return
            phase = "matrix_completion"
            output = ArchiveOutput(sys.stdout.buffer)
            coverage = asyncio.run(collect(catalog, application, polls, seconds=options.seconds,
                                          exchanges=options.exchanges, archive_bytes=options.archive_bytes, output=output))
            receipt.update({"status": "complete", "phase": "completed", "coverage": coverage,
                            "completion_sha256": output.digest.hexdigest()})
    except BaseException as error:
        receipt["phase"] = phase
        emit_unexpected(phase="verification", error=error,
                        effect="not_started" if phase == "input_admission" else "unconfirmed")
        print(RECEIPT.decode("ascii") + json.dumps(receipt, separators=(",", ":")), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    print(RECEIPT.decode("ascii") + json.dumps(receipt, separators=(",", ":")), file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
