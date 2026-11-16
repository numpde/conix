"""Serve the reviewed Fuji source through a disposable WARC-recording gateway."""

import json
import os
from pathlib import Path

import httpx

from http_recording import RecordingTransport
from rpc_gateway import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, serve
from stress_control import write_record

SOURCE = Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json"
ARCHIVE = Path("/capture/application.warc.gz")


def budget(name):
    raw = os.environ[name]
    if not raw.isdecimal() or not 0 < int(raw) < 2**31:
        raise ValueError("Capture limit must be an explicit positive integer.")
    return int(raw)


def main():
    source = json.loads(SOURCE.read_text())
    # The unmodified source snapshot owns upstream selection. No env URL or
    # campaign-specific default may silently redirect captured exchanges.
    count = budget("CONIX_STRESS_EXCHANGES")
    bytes_limit = budget("CONIX_STRESS_ARCHIVE_BYTES")
    record = None
    failure = None
    try:
        with ARCHIVE.open("xb") as output:
            record = RecordingTransport(
                httpx.HTTPTransport(retries=0, trust_env=False), output,
                max_request_bytes=MAX_REQUEST_BYTES,
                max_response_bytes=MAX_RESPONSE_BYTES,
                elapsed_budget_seconds=15,
                max_exchanges=count,
                max_archive_bytes=bytes_limit,
            )
            try:
                serve(upstream_url=source["rpc_url"], recording=record)
            except BaseException as error:
                failure = type(error).__name__
            finally:
                if not record.closed:
                    try:
                        record.close()
                    except BaseException as error:
                        failure = failure or type(error).__name__
                output.flush()
                os.fsync(output.fileno())
    except BaseException as error:
        failure = failure or type(error).__name__
    finally:
        if record is not None:
            completed = (failure is None and record.closed and not record.stopped
                         and not record.recording_failures and not record.cleanup_failures
                         and record.exchanges > 0)
            write_record("receipt.json", {
                "status": "complete" if completed else "partial",
                "exchanges": record.exchanges,
                "transport_stopped": record.stopped,
                "recording_failures": len(record.recording_failures),
                "cleanup_failures": len(record.cleanup_failures),
                "failure": failure,
            })
    if failure is not None:
        raise SystemExit("Capture gateway could not finalize.")


if __name__ == "__main__":
    main()
