"""Installed standalone indexer and explicit operator history commands."""

import argparse
import asyncio
import json
from pathlib import Path
import signal
import traceback

from conix.abi import MalformedABIData, ResourceExceeded
from conix.app import run
from conix.core import BranchConflict
from conix.control import request as request_control
from conix.history_cli import inspect_history, repair_history
from conix.input_staging import InputError, load_runtime_inputs
from conix.journal import CommitOutcomeUnknown, HistoryConflict, StorageUnavailable, WriterBusy
from conix.rpc import RpcProtocolError, RpcUnavailable


async def _run(catalog, dsn):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await run(catalog, dsn, stop)
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
    print(json.dumps({"outcome": "stopped", "source_id": catalog.source_id.hex()}), flush=True)


async def _execute(args):
    if args.operation == "control":
        response = await request_control(args.control_operation)
        print(json.dumps(response), flush=True)
        return

    (catalog, dsn) = load_runtime_inputs()
    if args.operation == "run":
        await _run(catalog, dsn)
    elif args.history_operation == "inspect":
        result = await inspect_history(catalog, dsn, ancestor=args.ancestor)
        print(json.dumps(result), flush=True)
    else:
        result = await repair_history(catalog, dsn, ancestor=args.ancestor)
        print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("run", help="Index continuously and follow the chain tip.")
    history = commands.add_parser("history", help="Inspect or explicitly repair stored history.")
    actions = history.add_subparsers(dest="history_operation", required=True)
    inspect = actions.add_parser("inspect", help="Inspect retained block identities without RPC.")
    inspect.add_argument("--ancestor", type=int, help="Inspect an exact stored height, instead of the latest 64.")
    repair = actions.add_parser("repair", help="Rewind to an explicitly selected canonical stored ancestor.")
    repair.add_argument("--ancestor", type=int, required=True)
    control = commands.add_parser(
        "control", help="Local private scheduling control inside variant C container.",
    )
    control.add_argument("control_operation", choices=("status", "pause", "resume", "step"))

    args = parser.parse_args()
    try:
        asyncio.run(_execute(args))
    except InputError as error:
        raise SystemExit(f"Conix refused staged inputs ({type(error).__name__}). {error}") from None
    except CommitOutcomeUnknown as error:
        raise SystemExit(f"Conix cannot confirm PostgreSQL COMMIT. {error}") from None
    except (BranchConflict, HistoryConflict, StorageUnavailable, WriterBusy,
            RpcProtocolError, RpcUnavailable, MalformedABIData, ResourceExceeded,
            TimeoutError, ValueError) as error:
        raise SystemExit(f"Conix {args.operation} failed ({type(error).__name__}). {error}") from None
    except OSError as error:
        raise SystemExit(
            f"Conix {args.operation} failed ({type(error).__name__}); "
            "inspect the installed service and selected local socket."
        ) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit("Conix operation was interrupted; inspect durable progress before restarting.") from None
    except Exception as error:
        location = traceback.extract_tb(error.__traceback__)[-1]
        raise SystemExit(
            f"Conix {args.operation} failed ({type(error).__name__}) "
            f"at {Path(location.filename).name}:{location.lineno}."
        ) from None


if __name__ == "__main__":
    main()
