"""Supply the Fuji campaign's existing source and header view to the RPC gateway."""

import json
import os
from pathlib import Path

from rpc_gateway import serve


def main():
    """Keep scenario selection here; the shared gateway has no Fuji defaults."""
    mode = os.environ.get("CONIX_FUJI_VIEW")
    if mode not in ("historical", "live"):
        raise SystemExit("Fuji gateway requires an explicit historical or live view.")
    source = json.loads((Path(__file__).parent / "fixtures/evm/fuji_pay_to_play.json").read_text())
    ceiling = source["initial_interval"]["end_block"] if mode == "historical" else None
    serve(upstream_url=source["rpc_url"], head_ceiling=ceiling)


if __name__ == "__main__":
    main()
