"""Run a finite amount of authoritative indexing in one Tier-0 writer session.

This is an independently useful manual catch-up operation, not a retry or
recovery supervisor. Each block still has its own bounded operation and
one journal transaction. The cycle's time budget is a *soft* limit checked
between blocks: never cancel a possibly committing transaction merely to
report a successful budget stop.
"""

import argparse
import asyncio
from dataclasses import dataclass
import json
import math
import time

from conix.core import open_writer
from conix.input_staging import load_runtime_inputs
from conix.journal import Progress


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Why a finite work cycle ended and what it actually published."""

    reason: str  # "at_tip" or "budget_exhausted"
    advanced_blocks: int
    last_progress: Progress | None


async def run_cycle(
    catalog,
    database_dsn,
    *,
    max_blocks: int,
    max_duration: float,
) -> CycleResult:
    """Execute successive real advances without waiting or recovering.

    Each advance has the configured individual operation deadline. A cycle
    budget never interrupts an active publication; exceptions, cancellation
    and unconfirmed COMMIT abort the cycle and invalidate the writer session.
    """
    if type(max_blocks) is not int or max_blocks < 1:
        raise ValueError("A work cycle requires a positive block budget.")
    if (
        type(max_duration) not in (int, float)
        or not math.isfinite(max_duration)
        or max_duration <= 0
    ):
        raise ValueError("A work cycle requires a finite positive duration.")

    started = time.monotonic()
    advanced_blocks = 0
    last_progress = None

    async with open_writer(
        catalog=catalog, database_dsn=database_dsn, rpc_url=catalog.chain.rpc_url,
    ) as writer:
        while advanced_blocks < max_blocks:
            if time.monotonic() - started >= max_duration:
                break

            async with asyncio.timeout(catalog.limits.operation_timeout_seconds):
                progress = await writer.advance_one()

            if progress is None:
                return CycleResult("at_tip", advanced_blocks, last_progress)

            advanced_blocks += 1
            last_progress = progress

    return CycleResult("budget_exhausted", advanced_blocks, last_progress)


def main() -> None:
    """Run a bounded cycle using exactly the already staged Tier-0 inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--max-blocks", type=int, required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    options = parser.parse_args()

    try:
        (catalog, database_dsn) = load_runtime_inputs()
        result = asyncio.run(run_cycle(
            catalog, database_dsn,
            max_blocks=options.max_blocks, max_duration=options.max_seconds,
        ))
        receipt = {
            "source_id": catalog.source_id.hex(),
            "outcome": result.reason,
            "advanced_blocks": result.advanced_blocks,
        }
        if result.last_progress is not None:
            receipt["block_number"] = result.last_progress.last_block.number
            receipt["block_hash"] = "0x" + result.last_progress.last_block.hash.hex()
            receipt["revision"] = result.last_progress.revision
        print(json.dumps(receipt), flush=True)
    except (Exception, KeyboardInterrupt) as error:
        # An interrupted COMMIT may have succeeded. An unconfirmed exit is
        # never a successful cycle receipt; durable state must be rechecked.
        raise SystemExit(
            f"Work cycle stopped ({type(error).__name__}). Inspect durable progress."
        ) from None


if __name__ == "__main__":
    main()
