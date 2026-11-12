"""Operator inspection and checked manual recovery of retained raw history.

Retained headers are database facts, not assertions about the live RPC
branch. Only the existing Writer.repair_to() checks canonical ancestry
and changes the journal.
"""

from conix.core import open_writer
from conix.journal import inspect_retained_ancestry


def _block(block):
    return {
        "number": block.number, "hash": "0x" + block.hash.hex(),
        "parent_hash": "0x" + block.parent_hash.hex(),
    }


async def inspect_history(catalog, database_dsn, *, ancestor=None):
    """Inspect a bounded, continuous stored prefix under writer exclusion."""
    limit = 1 if ancestor is not None else min(64, catalog.limits.max_evidence_blocks)
    result = await inspect_retained_ancestry(
        catalog=catalog, database_dsn=database_dsn, limit=limit, through=ancestor,
    )
    progress, blocks = result.progress, result.blocks
    return {
        "source_id": progress.source_id.hex(),
        "generation": str(progress.generation),
        "revision": progress.revision,
        "blocked": progress.blocked_reason is not None,
        "checkpoint": progress.last_block.number if progress.last_block else None,
        "blocks": [_block(block) for block in blocks],
        "more_older_blocks": bool(blocks and blocks[-1].number > progress.history_start),
    }


async def repair_history(catalog, database_dsn, *, ancestor):
    """Use a stored identity only through the original canonicality-checked core."""
    async with open_writer(
        catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
    ) as writer:
        ancestry = await writer.journal.retained_ancestry(limit=1, through=ancestor)
        progress = await writer.repair_to(ancestor=ancestry.blocks[0])
    return {
        "outcome": "repaired",
        "source_id": catalog.source_id.hex(),
        "ancestor": _block(progress.last_block),
        "generation": str(progress.generation),
        "revision": progress.revision,
    }
