"""Tier-0 commands: explicit source, one writer, no implicit repair or retries."""

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from conix.chain import Chain
from conix.configuration import load_settings, open_chain, read_dsn
from conix.engine import run_session
from conix.errors import Blocked, BranchConflict, ConfigurationError, CorruptStore, StateChanged, WriterBusy
from conix.journal import Journal
from conix.models import HistoryUnavailable, ObservationProtocolError, ObservationUnavailable
from conix.rpc import RpcError, RpcProtocolError, RpcUnavailable
from conix.schema import initialize


def _target(value: str) -> tuple[int, bytes]:
    """Parse an exact block height and hash for operator-directed repair."""
    try:
        (block_number, encoded_hash) = value.split(":")

        if (
            not block_number.isascii() or not block_number.isdecimal()
            or str(int(block_number)) != block_number
            or not 0 <= int(block_number) < 2**63
            or not encoded_hash.startswith("0x") or len(encoded_hash) != 66
        ):
            raise ValueError

        return int(block_number), bytes.fromhex(encoded_hash[2:])
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError(
            "Repair target must be NUMBER:0xHASH with canonical nonnegative decimal number and 32-byte hex hash."
        ) from error


def _parse(argv):
    parser = argparse.ArgumentParser(prog="conix", description="Follow exactly one configured PayToPlay collection stream.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    for command_name in ("init", "run", "status", "repair", "rebuild"):
        subparser = subcommands.add_parser(command_name)
        subparser.add_argument("--config", required=True, type=Path)

        if command_name == "init":
            subparser.add_argument("--owner-dsn-file", type=Path, required=True)

        if command_name == "repair":
            subparser.add_argument("--to", type=_target, required=True)

        if command_name == "rebuild":
            subparser.add_argument("--from-configured-origin", action="store_true", required=True)

    return parser.parse_args(argv)


async def _execute(arguments):
    """Use the owner credential only for initialization, never normal observation."""
    settings = load_settings(arguments.config)

    if arguments.command == "init":
        if arguments.owner_dsn_file == settings.database_dsn_file:
            raise ConfigurationError("Owner and runtime credential paths must be different; no schema writes with the worker credential.")

        await initialize(read_dsn(arguments.owner_dsn_file), settings)
        print("Verified owner-installed Conix schema and restricted runtime grants.")
        return

    journal = Journal(read_dsn(settings.database_dsn_file), settings)

    if arguments.command == "status":
        publication_basis = await journal.basis()

        status_document = {
            "chain_id": settings.deployment.chain_id,
            "contract_address": settings.deployment.address,
            "start_block": settings.start_block,
            "generation": str(publication_basis.generation),
            "revision": publication_basis.revision,
            "completed_block": (
                None if publication_basis.checkpoint is None else
                {"number": publication_basis.checkpoint.number, "hash": "0x" + publication_basis.checkpoint.hash.hex()}
            ),
            "collection_count": publication_basis.collection_count,
            "last_observed_upstream_head": (
                None if publication_basis.upstream_head is None else
                {"number": publication_basis.upstream_head.number, "hash": "0x" + publication_basis.upstream_head.hash.hex()}
            ),
            "head_observed_at": (
                None if publication_basis.head_observed_at is None else publication_basis.head_observed_at.isoformat()
            ),
            "blocked_reason": publication_basis.blocked_reason,
        }

        print(json.dumps(status_document, sort_keys=True))
        return

    async with open_chain(settings) as contract:
        chain = Chain(contract, settings)

        if arguments.command in ("repair", "rebuild"):
            async with journal.writer() as writer:
                publication_basis = await writer.basis()
                await chain.assert_source()

                repair_target = None
                if arguments.command == "repair":
                    (block_number, expected_block_hash) = arguments.to
                    repair_target = await chain.header(block_number)

                    if repair_target.hash != expected_block_hash:
                        raise BranchConflict(
                            f"Selected repair target {block_number}:0x{expected_block_hash.hex()} is not the source's canonical block."
                        )

                # The selected ancestor is only a proposal; the writer checks
                # canonicality again before mutating the durable prefix.
                await writer.repair_to(publication_basis, repair_target, chain)

            print("Conix history repaired; a fresh normal worker session must reindex the remaining blocks.")
            return

        # Only termination is handled here. Failures exit so a new worker must
        # establish ownership and re-read the PostgreSQL publication basis.
        stop_requested = asyncio.Event()
        event_loop = asyncio.get_running_loop()

        for termination_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                event_loop.add_signal_handler(termination_signal, stop_requested.set)
            except NotImplementedError:
                pass

        await run_session(journal, chain, stop_requested)


def main(argv=None):
    arguments = _parse(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        asyncio.run(_execute(arguments))
    except (
        ConfigurationError, CorruptStore, StateChanged, Blocked, BranchConflict,
        WriterBusy, ObservationProtocolError, ObservationUnavailable,
        HistoryUnavailable, RpcUnavailable, RpcProtocolError, RpcError,
        ValueError, TimeoutError, OSError,
    ) as error:
        # Never include DSN content or untrusted OS error text in the CLI.
        if isinstance(error, TimeoutError):
            safe_detail = "Operation deadline exceeded; inspect the selected RPC and database, then restart from durable progress."
        elif isinstance(error, OSError):
            safe_detail = f"{type(error).__name__}; inspect configured file, database and permissions."
        else:
            safe_detail = str(error)

        raise SystemExit(f"conix {arguments.command} failed ({type(error).__name__}): {safe_detail}") from None


if __name__ == "__main__":
    main()
