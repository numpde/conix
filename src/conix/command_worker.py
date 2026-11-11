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
from conix.worker import advance, progress_receipt


MAX_COMMAND_BYTES = 128


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
    if (
        type(document) is not dict
        or set(document) != {"version", "action"}
        or type(document["version"]) is not int
        or document["version"] != 1
        or document["action"] not in ("advance", "stop")
    ):
        raise CommandProtocolError("A command has an unsupported action or version.")
    return document["action"]


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
