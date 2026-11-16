"""Independent networkless inspection of raw WARC bytes and exchange linkage.

Does not import the recorder, Conix scanner or journal. Prints bounded
control facts, never archived request/response contents.
"""

import hashlib
import json
from pathlib import Path
import sys

from stress_archive import read_archive

CAPTURE = Path("/capture")


def inspect(name):
    if name not in ("application", "observer"):
        raise ValueError("Unknown capture stream.")
    path = CAPTURE / (name + ".warc.gz")
    source = json.loads((Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json").read_text())
    first_header = None
    exchanges = read_archive(path, source["rpc_url"])
    if name == "observer":
        for exchange in exchanges:
            try:
                header = exchange.result()
                first_header = {
                    "id": exchange.request["id"], "height": int(header["number"], 16),
                    "hash": header["hash"], "parent_hash": header["parentHash"],
                    "body_sha256": hashlib.sha256(exchange.body).hexdigest(),
                }
                break
            except (ValueError, KeyError, TypeError):
                continue
    if name == "observer" and (len(exchanges) < 3 or first_header is None):
        raise ValueError("Observer archive lacks seed and boundary observations.")
    return {"status": "verified", "stream": name, "exchanges": len(exchanges), "first_header": first_header}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Select one archive.")
        print(json.dumps(inspect(sys.argv[1]), sort_keys=True, separators=(",", ":")))
    except (OSError, ValueError):
        raise SystemExit("Raw archive inspection failed.") from None
