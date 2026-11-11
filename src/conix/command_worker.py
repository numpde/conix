"""Serve bounded, sequential advance commands using the installed Tier-0 worker.

The command process owns no additional scanner or durable state. Every advance
opens and retires an ordinary Tier-0 session; an uncertain outcome terminates
the process rather than accepting another command. This protocol uses stdin
and stdout within a confined container, never a host-exposed network port.
"""

import asyncio
import json
import sys

from conix.input_staging import load_runtime_inputs
from conix.journal import inspect_retained_ancestry
from conix.worker import advance, progress_receipt


MAX_COMMAND_BYTES = 128
MAX_INSPECT_BLOCKS = 8  # Keep the response inside the host-side 4096-byte bound.


class CommandProtocolError(ValueError):
    """A malformed command cannot trigger any indexer operation."""


def _object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise CommandProtocolError("A command repeated one of its fields.")
    return value


def _command(line: bytes) -> str:
    """Admit exactly one small, newline-terminated command frame."""
    if not 0 < len(line) <= MAX_COMMAND_BYTES or not line.endswith(b"\n"):
        raise CommandProtocolError("A command is incomplete or exceeds its bound.")
    try:
        document = json.loads(line, object_pairs_hook=_object)
    except (ValueError, UnicodeError) as error:
        raise CommandProtocolError("A command is not valid JSON.") from error
    if type(document) is not dict or type(document.get("version")) is not int or document["version"] != 1:
        raise CommandProtocolError("A command has an unsupported version.")

    action = document.get("action")
    if action in ("advance", "stop") and set(document) == {"version", "action"}:
        return action
    if action == "inspect" and set(document) == {"version", "action", "limit", "through"}:
        limit, through = document["limit"], document["through"]
        if (
            type(limit) is int and 1 <= limit <= MAX_INSPECT_BLOCKS
            and (through is None or type(through) is int and 0 <= through < 2**63)
        ):
            return (action, limit, through)

    raise CommandProtocolError("A command has an unsupported action or arguments.")


def _write(stream, record):
    """One complete response per command; do not acknowledge a failed write."""
    stream.write((json.dumps({"version": 1, **record}, separators=(",", ":")) + "\n").encode("ascii"))
    stream.flush()


async def serve(catalog, database_dsn, *, incoming, outgoing):
    """Process one command at a time; never pipeline unconfirmed advances."""
    while raw := incoming.readline(MAX_COMMAND_BYTES + 1):
        action = _command(raw)
        if action == "stop":
            _write(outgoing, {"outcome": "stopped"})
            return

        if isinstance(action, tuple):
            (_, limit, through) = action
            ancestry = await inspect_retained_ancestry(
                catalog=catalog, database_dsn=database_dsn, limit=limit, through=through,
            )
            # Journal facts are not a decision that the chain is canonical.
            _write(outgoing, {
                "outcome": "inspected",
                "source_id": ancestry.progress.source_id.hex(),
                "revision": ancestry.progress.revision,
                "generation": str(ancestry.progress.generation),
                "history_start": ancestry.progress.history_start,
                "blocked": ancestry.progress.blocked_reason is not None,
                "last_block_number": (
                    None if ancestry.progress.last_block is None
                    else ancestry.progress.last_block.number
                ),
                "blocks": [
                    {"number": block.number, "hash": "0x" + block.hash.hex()}
                    for block in ancestry.blocks
                ],
            })
            continue

        # Only this existing method is authorized to validate and publish.
        progress = await advance(catalog, database_dsn)
        _write(outgoing, progress_receipt(catalog, progress))


def main():
    if sys.argv[1:] != ["serve"]:
        raise SystemExit("Use python -I -m conix.command_worker serve with staged inputs.")

    try:
        (catalog, database_dsn) = load_runtime_inputs()
        asyncio.run(serve(catalog, database_dsn, incoming=sys.stdin.buffer,
                          outgoing=sys.stdout.buffer))
    except (Exception, KeyboardInterrupt) as error:
        # The process stops on any uncertainty. Never echo credentials, RPC
        # error text, or arbitrary exception prose into container stderr.
        raise SystemExit(
            f"Command worker stopped ({type(error).__name__}); inspect durable progress."
        ) from None


if __name__ == "__main__":
    main()
