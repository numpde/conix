"""Independent networkless inspection of raw WARC bytes and exchange linkage.

Does not import the recorder, Conix scanner or journal. Prints bounded
control facts, never archived request/response contents.
"""

import hashlib
import json
from pathlib import Path
import sys

from warcio.archiveiterator import ArchiveIterator

CAPTURE = Path("/capture")
LIMIT = 2 * 1024 * 1024 + 16384


def inspect(name):
    if name not in ("application", "observer"):
        raise ValueError("Unknown capture stream.")
    path = CAPTURE / (name + ".warc.gz")
    requests = {}
    responses = set()
    metadata = set()
    first_header = None
    with path.open("rb") as archive:
        for record in ArchiveIterator(archive, check_digests=True):
            if record.digest_checker.problems:
                raise ValueError("WARC digest verification failed.")
            kind = record.rec_type
            if kind == "warcinfo":
                continue
            if kind not in ("request", "response", "metadata"):
                raise ValueError("Unexpected WARC record type.")
            record_id = record.rec_headers.get_header("WARC-Record-ID")
            if not record_id:
                raise ValueError("WARC record lacks identity.")
            raw = record.raw_stream.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError("WARC exchange exceeds admitted response budget.")
            if kind == "metadata":
                linked = record.rec_headers.get_header("WARC-Refers-To")
                if not linked or linked in metadata:
                    raise ValueError("WARC metadata linkage is ambiguous.")
                metadata.add(linked)
            elif kind == "request":
                if record_id in requests:
                    raise ValueError("Repeated WARC request identity.")
                requests[record_id] = record.rec_headers.get_header("WARC-Target-URI")
            else:
                linked = record.rec_headers.get_header("WARC-Concurrent-To")
                target = record.rec_headers.get_header("WARC-Target-URI")
                if linked not in requests or target != requests[linked] or linked in responses:
                    raise ValueError("WARC response does not match one recorded request.")
                responses.add(linked)
                if name == "observer" and first_header is None and record.http_headers.get_statuscode() == "200":
                    try:
                        decoded = json.loads(raw)
                        header = decoded["result"]
                        first_header = {
                            "id": decoded["id"], "height": int(header["number"], 16),
                            "hash": header["hash"], "parent_hash": header["parentHash"],
                            "body_sha256": hashlib.sha256(raw).hexdigest(),
                        }
                    except (ValueError, KeyError, TypeError):
                        pass
    if not requests or set(requests) != responses or not requests.issubset(metadata):
        raise ValueError("WARC contains incomplete request/response/metadata pairs.")
    if name == "observer" and (len(requests) < 3 or first_header is None):
        raise ValueError("Observer archive lacks seed and boundary observations.")
    return {"status": "verified", "stream": name, "exchanges": len(requests), "first_header": first_header}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Select one archive.")
        print(json.dumps(inspect(sys.argv[1]), sort_keys=True, separators=(",", ":")))
    except (OSError, ValueError):
        raise SystemExit("Raw archive inspection failed.") from None
