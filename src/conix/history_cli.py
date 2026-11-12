"""Operator inspection and checked manual recovery of retained raw history.

Inspection deliberately reads the durable journal without demanding that
stored blocks are still canonical. Only the existing Writer.repair_to can
change that history and it independently verifies the chosen ancestor.
"""

from conix.core import open_writer
from conix.journal import HistoryConflict, open_journal


def _block(block):
    return {
        "number": block.number, "hash": "0x" + block.hash.hex(),
        "parent_hash": "0x" + block.parent_hash.hex(),
    }


async def inspect_history(catalog, database_dsn, *, ancestor=None):
    """Report stored identities even if the chain has reorganized or gone away."""
    async with open_journal(catalog=catalog, database_dsn=database_dsn) as journal:
        if ancestor is None:
            blocks = await journal.retained_blocks(limit=65)
            truncated = len(blocks) > 64
            blocks = blocks[:64]
        else:
            stored = await journal.retained_block(ancestor)
            if stored is None:
                raise HistoryConflict("Selected ancestor is not retained.")
            blocks, truncated = (stored,), False

        return {
            "source_id": catalog.source_id.hex(),
            "generation": str(journal.basis.generation),
            "revision": journal.basis.revision,
            "blocked": journal.basis.blocked_reason is not None,
            "checkpoint": (
                journal.basis.last_block.number if journal.basis.last_block else None
            ),
            "blocks": [_block(block) for block in blocks],
            "more_older_blocks": truncated,
        }


async def repair_history(catalog, database_dsn, *, ancestor):
    """Repair through the same RPC-checked writer and fenced journal as Tier 0."""
    async with open_writer(
        catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
    ) as writer:
        stored = await writer.journal.retained_block(ancestor)
        if stored is None:
            raise HistoryConflict("Selected repair ancestor is not retained.")
        progress = await writer.repair_to(ancestor=stored)
    return {
        "outcome": "repaired",
        "source_id": catalog.source_id.hex(),
        "ancestor": _block(progress.last_block),
        "generation": str(progress.generation),
        "revision": progress.revision,
    }
