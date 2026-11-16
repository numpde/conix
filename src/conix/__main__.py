"""Installed standalone indexer and explicit operator history commands."""

import argparse
import asyncio
import json
import signal

from conix.app import run
from conix.history_cli import inspect_history, repair_history
from conix.input_staging import load_runtime_inputs
from conix.journal import CommitOutcomeUnknown
from conix.process_diagnostic import emit, emit_unexpected


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


async def _execute(args, catalog, dsn):
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

    args = parser.parse_args()
    phase = "staged_inputs"
    try:
        catalog, dsn = load_runtime_inputs()
        phase = "run" if args.operation == "run" else "history_" + args.history_operation
        asyncio.run(_execute(args, catalog, dsn))
    except CommitOutcomeUnknown:
        emit(phase=phase, cause="CommitOutcomeUnknown", effect="commit_unknown")
        raise SystemExit(2) from None
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:
        emit_unexpected(phase=phase, error=error,
                        effect="not_started" if phase == "staged_inputs" else "unconfirmed")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
