"""Run one foreground advance using the installed Tier-0 writer."""

import asyncio
import json
from pathlib import Path
import sys
import traceback

from conix.abi import MalformedABIData, ResourceExceeded
from conix.core import BranchConflict, open_writer
from conix.input_staging import InputError, load_runtime_inputs
from conix.journal import CommitOutcomeUnknown, HistoryConflict, StorageUnavailable, WriterBusy
from conix.rpc import RpcProtocolError, RpcUnavailable


async def advance(catalog, database_dsn):
    """Use one work deadline and cancel the session if it expires."""
    async with asyncio.timeout(catalog.limits.operation_timeout_seconds):
        async with open_writer(
            catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
        ) as writer:
            return await writer.advance_one()


def main():
    if sys.argv[1:] != ["advance"]:
        raise SystemExit("Use python -I -m conix.worker advance with staged inputs.")

    phase = "staged inputs"
    try:
        (catalog, database_dsn) = load_runtime_inputs()
        phase = "advance"
        progress = asyncio.run(advance(catalog, database_dsn))
    except InputError as error:
        raise SystemExit(f"Cannot load staged inputs ({type(error).__name__}). {error}") from None
    except CommitOutcomeUnknown as error:
        raise SystemExit(f"Index advance has an unconfirmed PostgreSQL COMMIT. {error}") from None
    except (TimeoutError, KeyboardInterrupt) as error:
        raise SystemExit(
            f"Index advance was interrupted ({type(error).__name__}). "
            "Reopen the journal and inspect durable progress before another advance."
        ) from None
    except (BranchConflict, HistoryConflict, StorageUnavailable, WriterBusy,
            RpcProtocolError, RpcUnavailable, MalformedABIData, ResourceExceeded) as error:
        raise SystemExit(f"Index advance failed ({type(error).__name__}). {error}") from None
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        raise SystemExit(
            f"Index advance failed during {phase} ({type(error).__name__}) "
            f"at {Path(frame.filename).name}:{frame.lineno}."
        ) from None

    receipt = {"outcome": "at_tip", "source_id": catalog.source_id.hex()}
    if progress is not None:
        receipt.update(
            outcome="advanced", block_number=progress.last_block.number,
            block_hash="0x" + progress.last_block.hash.hex(), revision=progress.revision,
        )
    try:
        print(json.dumps(receipt), flush=True)
    except OSError as error:
        raise SystemExit(f"Index advance completed but receipt output failed ({type(error).__name__}).") from None


if __name__ == "__main__":
    main()
