"""Observe the reviewed Fuji head independently of the original Conix process.

Seed acquisition precedes named deployment configuration. The released
observation timeline contains actual poll intervals, never inferred arrivals.
"""

import json
import math
import os
from pathlib import Path
import time

import httpx

from conix.core import _block
from http_recording import RecordingTransport
from stress_control import CAPTURE, write_record

SOURCE = Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json"
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def integer(name):
    value = os.environ[name]
    if not value.isdecimal() or not 0 < int(value) < 2**31:
        raise ValueError("Observation budget requires an explicit positive integer.")
    return int(value)


def observe(client, source, identifier):
    """One real request, with response completion distinguished from header receipt."""
    started = time.monotonic_ns()
    response_time = None
    status = "unavailable"
    failure = None
    header = None
    try:
        with client.stream("POST", source["rpc_url"], json={
            "jsonrpc": "2.0", "id": identifier,
            "method": "eth_getBlockByNumber", "params": ["latest", False],
        }, headers={"Accept-Encoding": "identity"}) as response:
            # The recording transport buffers before exposing headers. This is
            # caller-visible completion, NOT physical provider header latency.
            response_time = time.monotonic_ns()
            body = response.read()
            if response.status_code != 200:
                failure = "http_status"
            else:
                try:
                    reply = json.loads(body)
                    if (type(reply) is not dict or reply.get("jsonrpc") != "2.0"
                            or reply.get("id") != identifier or "error" in reply
                            or type(reply.get("result")) is not dict):
                        failure = "rpc_envelope"
                    else:
                        raw = reply["result"]
                        height = int(raw["number"], 16)
                        block = _block(raw, height)
                        header = {
                            "height": block.header.number,
                            "hash": "0x" + block.header.hash.hex(),
                            "parent_hash": "0x" + block.header.parent_hash.hex(),
                        }
                        status = "observed"
                except (ValueError, TypeError, KeyError, AttributeError):
                    failure = "malformed_header"
    except (httpx.HTTPError, OSError) as error:
        failure = type(error).__name__
    finished = time.monotonic_ns()
    return {
        "id": identifier, "status": status, "failure": failure,
        "started_ns": started, "response_ns": response_time,
        "finished_ns": finished, "header": header,
    }


def admit_window(samples, maximum_gap_ns):
    """Return observed timing precision; unavailable samples never become null heads."""
    if len(samples) < 2 or samples[0]["status"] != "observed" or samples[-1]["status"] != "observed":
        return {"status": "unavailable", "reason": "boundary_observation"}
    previous = None
    longest_gap = 0
    by_height = {}
    for sample in samples:
        if not sample["started_ns"] <= sample["finished_ns"]:
            return {"status": "unavailable", "reason": "unordered_timing"}
        if previous is not None and sample["started_ns"] < previous["started_ns"]:
            return {"status": "unavailable", "reason": "unordered_timing"}
        if sample["status"] == "observed":
            if previous is not None and previous.get("last_success") is not None:
                gap = sample["finished_ns"] - previous["last_success"]
                longest_gap = max(gap, longest_gap)
            header = sample["header"]
            prior = by_height.setdefault(header["height"], (header["hash"], header["parent_hash"]))
            if prior != (header["hash"], header["parent_hash"]):
                return {"status": "unavailable", "reason": "conflicting_head"}
        previous = dict(sample, last_success=(sample["finished_ns"] if sample["status"] == "observed"
                                               else previous.get("last_success") if previous else None))
    good = [sample for sample in samples if sample["status"] == "observed"]
    heights = [sample["header"]["height"] for sample in good]
    if heights != sorted(heights):
        return {"status": "unavailable", "reason": "head_decrease"}
    if longest_gap > maximum_gap_ns:
        return {"status": "unavailable", "reason": "observation_gap", "longest_gap_ns": longest_gap}
    if heights[-1] <= heights[0]:
        return {"status": "unavailable", "reason": "head_not_advanced", "longest_gap_ns": longest_gap}
    return {"status": "complete", "reason": None, "longest_gap_ns": longest_gap}


def run():
    source = json.loads(SOURCE.read_text())
    duration = integer("CONIX_STRESS_DURATION_SECONDS")
    cadence_ms = integer("CONIX_STRESS_CADENCE_MS")
    gap_ms = integer("CONIX_STRESS_MAX_GAP_MS")
    start_wait = integer("CONIX_STRESS_START_WAIT_SECONDS")
    count = integer("CONIX_STRESS_EXCHANGES")
    limit = integer("CONIX_STRESS_ARCHIVE_BYTES")
    cadence_ns = cadence_ms * 1000000
    gap_ns = gap_ms * 1000000
    if math.ceil(duration * 1000 / cadence_ms) + 2 > count:
        raise ValueError("Observer exchange budget cannot cover the selected observation window.")
    recorder = None
    observation = None
    reason = None
    try:
        with (CAPTURE / "observer.warc.gz").open("xb") as archive:
            recorder = RecordingTransport(
                httpx.HTTPTransport(retries=0, trust_env=False), archive,
                max_request_bytes=MAX_REQUEST_BYTES, max_response_bytes=MAX_RESPONSE_BYTES,
                elapsed_budget_seconds=15, max_exchanges=count, max_archive_bytes=limit,
            )
            with httpx.Client(transport=recorder, timeout=10, trust_env=False, follow_redirects=False) as client:
                seed = observe(client, source, 1)
                if seed["status"] != "observed" or recorder.stopped:
                    raise ValueError("Capture seed observation is unavailable.")
                write_record("seed.json", seed)
                deadline = time.monotonic() + start_wait
                while not (CAPTURE / "release").exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Observation release was not received.")
                    time.sleep(0.05)
                released_ns = time.monotonic_ns()
                deadline_ns = released_ns + duration * 1000000000
                samples = []
                index = 0
                while True:
                    scheduled = released_ns + index * cadence_ns
                    if scheduled >= deadline_ns:
                        break
                    now = time.monotonic_ns()
                    if now < scheduled:
                        time.sleep((scheduled - now) / 1000000000)
                    samples.append(observe(client, source, 2 + len(samples)))
                    if recorder.stopped:
                        raise RuntimeError("Observer recorder stopped.")
                    index = max(index + 1, (time.monotonic_ns() - released_ns) // cadence_ns + 1)
                # The final poll starts at or after the nominal deadline.
                now = time.monotonic_ns()
                if now < deadline_ns:
                    time.sleep((deadline_ns - now) / 1000000000)
                samples.append(observe(client, source, 2 + len(samples)))
                if recorder.stopped:
                    raise RuntimeError("Observer recorder stopped.")
                verdict = admit_window(samples, gap_ns)
                observation = {
                    "seed": seed, "released_ns": released_ns,
                    "requested_duration_seconds": duration, "cadence_ms": cadence_ms,
                    "maximum_gap_ms": gap_ms, "actual_span_ns": (
                        samples[-1]["finished_ns"] - samples[0]["started_ns"]),
                    "samples": samples, "admission": verdict,
                }
                write_record("observations.json", observation)
            archive.flush()
            os.fsync(archive.fileno())
    except BaseException as error:
        reason = type(error).__name__
    finally:
        if recorder is not None:
            complete = (reason is None and observation is not None
                        and observation["admission"]["status"] == "complete"
                        and recorder.closed and not recorder.stopped
                        and not recorder.recording_failures and not recorder.cleanup_failures)
            write_record("receipt.json", {
                "status": "complete" if complete else "partial", "failure": reason,
                "exchanges": recorder.exchanges,
                "transport_stopped": recorder.stopped,
                "recording_failures": len(recorder.recording_failures),
                "cleanup_failures": len(recorder.cleanup_failures),
            })
    if reason is not None:
        raise SystemExit("Head observation could not complete.")


if __name__ == "__main__":
    run()
