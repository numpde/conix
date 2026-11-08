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
from conix.models import Block, HistoryUnavailable, ObservationProtocolError, ObservationUnavailable
from conix.rpc import RpcError, RpcProtocolError, RpcUnavailable
from conix.schema import initialize

logger = logging.getLogger("conix")


def _target(value: str) -> tuple[int, bytes]:
    try:
        number, encoded = value.split(":")
        if (
            not number.isascii() or not number.isdecimal()
            or str(int(number)) != number or not 0 <= int(number) < 2**63
            or not encoded.startswith("0x") or len(encoded) != 66
        ):
            raise ValueError
        return int(number), bytes.fromhex(encoded[2:])
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError(
            "Repair target must be NUMBER:0xHASH with canonical nonnegative decimal number and 32-byte hex hash."
        ) from error


def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="conix", description="Follow exactly one configured PayToPlay collection stream."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "run", "status", "repair", "rebuild"):
        entry = subcommands.add_parser(command)
        entry.add_argument("--config", required=True, type=Path)
        if command == "init":
            entry.add_argument("--owner-dsn-file", type=Path, required=True)
        if command == "repair":
            entry.add_argument("--to", type=_target, required=True)
        if command == "rebuild":
            entry.add_argument("--from-configured-origin", action="store_true", required=True)
    return parser.parse_args(argv)


async def _execute(arguments):
    settings = load_settings(arguments.config)
    if arguments.command == "init":
        if arguments.owner_dsn_file == settings.database_dsn_file:
            raise ConfigurationError(
                "Owner and runtime credential paths must be different; no schema writes with the worker credential."
            )
        await initialize(read_dsn(arguments.owner_dsn_file), settings)
        print("Verified owner-installed Conix schema and restricted runtime grants.")
        return
    journal = Journal(read_dsn(settings.database_dsn_file), settings)

    if arguments.command == "status":
        basis = await journal.basis()
        answer = {
            "chain_id": settings.deployment.chain_id,
            "contract_address": settings.deployment.address,
            "start_block": settings.start_block,
            "generation": str(basis.generation),
            "revision": basis.revision,
            "completed_block": (
                None if basis.checkpoint is None else
                {"number": basis.checkpoint.number, "hash": "0x" + basis.checkpoint.hash.hex()}
            ),
            "collection_count": basis.collection_count,
            "last_observed_upstream_head": (
                None if basis.upstream_head is None else
                {"number": basis.upstream_head.number, "hash": "0x" + basis.upstream_head.hash.hex()}
            ),
            "head_observed_at": None if basis.head_observed_at is None else basis.head_observed_at.isoformat(),
            "blocked_reason": basis.blocked_reason,
        }
        print(json.dumps(answer, sort_keys=True))
        return

    async with open_chain(settings) as contract:
        chain = Chain(contract, settings)
        if arguments.command in ("repair", "rebuild"):
            async with journal.writer() as writer:
                basis = await writer.basis()
                await chain.assert_source()
                target = None
                if arguments.command == "repair":
                    number, block_hash = arguments.to
                    target = await chain.header(number)
                    if target.hash != block_hash:
                        raise BranchConflict(
                            f"Selected repair target {number}:0x{block_hash.hex()} is not the source's canonical block."
                        )
                # Same concrete repair primitive called by future Tier-1 controller.
                await writer.repair_to(basis, target, chain)
            print("Conix history repaired; a fresh normal worker session must reindex the remaining blocks.")
            return

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(name, stop.set)
            except NotImplementedError:
                pass
        await run_session(journal, chain, stop)


def main(argv=None):
    arguments = _parse(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_execute(arguments))
    except (ConfigurationError, CorruptStore, StateChanged, Blocked, BranchConflict,
            WriterBusy, ObservationProtocolError, ObservationUnavailable,
            HistoryUnavailable, RpcUnavailable, RpcProtocolError, RpcError,
            ValueError, TimeoutError, OSError) as error:
        # The operator owns the DSN file; never show its contents or the DSN.
        if isinstance(error, TimeoutError):
            detail = "Operation deadline exceeded; inspect the selected RPC and database, then restart from durable progress."
        elif isinstance(error, OSError):
            detail = f"{type(error).__name__}; inspect configured file, database and permissions."
        else:
            detail = str(error)
        raise SystemExit(f"conix {arguments.command} failed: {detail}") from None


if __name__ == "__main__":
    main()
