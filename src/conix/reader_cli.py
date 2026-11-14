"""One-shot Reader admission for an independently prepared named snapshot.

A Reader is a client library, not a background service. This command checks
the actual selected chain and SELECT-only PostgreSQL role, then exits.
"""

import asyncio
import json
import sys

from conix.input_staging import load_runtime_inputs
from conix.process_diagnostic import emit_unexpected
from conix.reader import open_reader


async def inspect(catalog, reader_dsn):
    """Open the original checked Reader and report its durable checkpoint."""
    async with open_reader(
        catalog=catalog,
        database_dsn=reader_dsn,
        rpc_url=catalog.chain.rpc_url,
    ) as reader:
        checkpoint = await reader.checkpoint()
    return {
        "outcome": "reader_ready",
        "source_id": catalog.source_id.hex(),
        "checkpoint": checkpoint.block.number if checkpoint is not None else None,
        "generation": str(checkpoint.generation) if checkpoint is not None else None,
    }


def main():
    """Check a privately staged Reader; never print configuration or DSN text."""
    if sys.argv[1:] != ["check"]:
        raise SystemExit("Use python -m conix.reader_cli check.")
    try:
        catalog, reader_dsn = load_runtime_inputs()
        record = asyncio.run(inspect(catalog, reader_dsn))
    except Exception as error:
        emit_unexpected(phase="reader_check", error=error, effect="not_started")
        raise SystemExit(2) from None
    print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
