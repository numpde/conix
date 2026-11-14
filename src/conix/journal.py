"""One exclusive PostgreSQL writer for the selected complete-block prefix.

This journal stores raw events supplied by the concrete core after its RPC
completeness and ABI checks. It does not acquire evidence or infer completeness
from a caller's event list. It owns atomic publication, durable progress,
writer exclusion and checked manual suffix repair.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection

from conix.catalog import Catalog


# One writer per schema/database, regardless of configured contract selection.
WRITER_LOCK = 0x434F4E4958

class WriterBusy(RuntimeError):
    """Another session owns the selected database's raw-history publisher."""


class HistoryConflict(RuntimeError):
    """The proposed mutation contradicts the retained source or checkpoint."""


class StorageUnavailable(RuntimeError):
    """A PostgreSQL operation failed without establishing a new publication."""


class CommitOutcomeUnknown(RuntimeError):
    """The writer lost confirmation of COMMIT and must be retired."""


@dataclass(frozen=True, slots=True)
class Block:
    number: int
    hash: bytes
    parent_hash: bytes
    timestamp: int


@dataclass(frozen=True, slots=True)
class RawEvent:
    transaction_hash: bytes
    transaction_index: int
    log_index: int
    address: bytes
    topics: tuple[bytes, ...]
    data: bytes


@dataclass(frozen=True, slots=True)
class Progress:
    source_id: bytes
    generation: UUID
    history_start: int
    revision: int
    last_block: Block | None
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class RetainedAncestry:
    """Bounded stored headers tied to a freshly observed durable basis.

    These headers are journal facts, NOT an assertion that their branch is
    currently canonical at an RPC provider. A later repair revalidates its
    selected ancestor through the ordinary checked core method.
    """

    progress: Progress
    blocks: tuple[Block, ...]


class Journal:
    """The only concrete durable mutation path used by future scanners."""

    def __init__(self, connection: AsyncConnection, catalog: Catalog, basis: Progress):
        self.connection = connection
        self.catalog = catalog
        self.basis = basis
        self.retired = False

    @classmethod
    async def open(cls, *, catalog: Catalog, database_dsn: str) -> Journal:
        """Inspect durable identity and acquire the database-wide writer lock."""
        # A reader-source projection may compile the same event identity,
        # but must never be admitted as a writer or acquire its lock.
        if catalog.database.runtime_role != "conix_runtime":
            raise StorageUnavailable("Writer requires the dedicated conix_runtime role.")
        try:
            connection = await AsyncConnection.connect(
                database_dsn,
                connect_timeout=5,
                autocommit=True,
            )
        except psycopg.Error as error:
            raise StorageUnavailable("Cannot open the selected runtime PostgreSQL connection.") from error

        try:
            identity = await (
                await connection.execute("SELECT current_user")
            ).fetchone()
            if identity != (catalog.database.runtime_role,):
                raise StorageUnavailable("Selected connection does not use the configured runtime role.")

            acquired = await (
                await connection.execute("SELECT pg_try_advisory_lock(%s)", (WRITER_LOCK,))
            ).fetchone()
            if acquired != (True,):
                raise WriterBusy("The selected raw-history database has an active writer.")

            # A blocked history is inspectable and may be repaired, but no
            # further block can be published until the repair succeeds.
            basis = await cls._read_basis(connection=connection, catalog=catalog)
        except BaseException as error:
            await connection.close()
            if isinstance(error, psycopg.Error):
                raise StorageUnavailable("Cannot inspect the selected journal's durable basis.") from error
            raise

        return cls(connection, catalog, basis)

    @staticmethod
    async def _read_basis(*, connection: AsyncConnection, catalog: Catalog) -> Progress:
        record = await (
            await connection.execute(
                "SELECT source_id, chain_id, genesis_hash, history_start, "
                "generation, revision, last_block, blocked_reason "
                "FROM conix.source_progress WHERE singleton = 1"
            )
        ).fetchone()

        if record is None:
            raise HistoryConflict("Selected database has no initialized source history.")

        (
            source_id, chain_id, genesis_hash, history_start,
            generation, revision, last_number, blocked_reason,
        ) = record
        if (
            source_id != catalog.source_id
            or chain_id != catalog.chain.chain_id
            or genesis_hash != catalog.chain.genesis_hash
            or history_start != catalog.chain.start_block
        ):
            raise HistoryConflict("Retained history belongs to a different selected source.")

        last_block = None
        if last_number is not None:
            row = await (
                await connection.execute(
                    "SELECT number, hash, parent_hash, timestamp "
                    "FROM conix.blocks WHERE number = %s",
                    (last_number,),
                )
            ).fetchone()
            if row is None:
                raise HistoryConflict("Durable checkpoint references a missing indexed block.")
            last_block = Block(*row)

        return Progress(
            source_id=source_id,
            generation=generation,
            history_start=history_start,
            revision=revision,
            last_block=last_block,
            blocked_reason=blocked_reason,
        )

    async def inspect(self) -> Progress:
        """Read the retained basis without changing publication ownership."""
        if self.retired:
            raise HistoryConflict("This writer has retired; open a fresh session.")
        return await self._read_basis(connection=self.connection, catalog=self.catalog)

    async def retained_ancestry(
        self, *, limit: int, through: int | None = None,
    ) -> RetainedAncestry:
        """Read recent stored block identities, even if history is blocked.

        The journal's exclusive writer lock fences this snapshot against
        authorized publication and repair. No RPC interpretation or repair
        policy is applied to the returned records.
        """
        if type(limit) is not int or not 1 <= limit <= self.catalog.limits.max_evidence_blocks:
            raise ValueError("Retained ancestry limit exceeds the configured evidence budget.")

        progress = await self.inspect()
        if progress.last_block is None:
            if through is not None:
                raise HistoryConflict("No retained block exists at the selected height.")
            return RetainedAncestry(progress, ())

        highest = progress.last_block.number
        if through is not None:
            if type(through) is not int or not progress.history_start <= through <= highest:
                raise HistoryConflict("Selected retained height is outside the complete prefix.")
            highest = through

        lowest = max(progress.history_start, highest - limit + 1)
        try:
            records = await (
                await self.connection.execute(
                    "SELECT number, hash, parent_hash, timestamp FROM conix.blocks "
                    "WHERE number BETWEEN %s AND %s ORDER BY number DESC",
                    (lowest, highest),
                )
            ).fetchall()
        except psycopg.Error as error:
            raise StorageUnavailable("Cannot inspect retained ancestry blocks.") from error

        blocks = tuple(Block(*record) for record in records)
        if tuple(block.number for block in blocks) != tuple(range(highest, lowest - 1, -1)):
            raise HistoryConflict("Retained ancestry contains an incomplete block interval.")

        return RetainedAncestry(progress, blocks)

    @asynccontextmanager
    async def _transaction(self):
        if self.retired:
            raise HistoryConflict("This writer has retired; open a fresh session.")

        try:
            await self.connection.execute("BEGIN")
            try:
                await self.connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(int(self.catalog.limits.operation_timeout_seconds * 1000)),),
                )
                yield
            except BaseException:
                try:
                    await self.connection.execute("ROLLBACK")
                except BaseException:
                    # A failed rollback invalidates this session even when
                    # the original operation failed before COMMIT.
                    self.retired = True
                    await self.connection.close()
                raise

            # This is the only ambiguous boundary. Do not retry or assume
            # that a connection error means PostgreSQL rejected the COMMIT.
            try:
                await self.connection.execute("COMMIT")
            except BaseException as error:
                self.retired = True
                try:
                    await self.connection.close()
                except BaseException:
                    pass
                if isinstance(error, Exception):
                    raise CommitOutcomeUnknown(
                        "PostgreSQL COMMIT was not confirmed; reopen and inspect durable progress."
                    ) from error
                raise
        except psycopg.Error as error:
            raise StorageUnavailable(
                f"PostgreSQL journal operation failed (SQLSTATE={error.sqlstate})."
            ) from error

    async def _advance_progress(self, *, last_number: int, generation: UUID) -> None:
        """Atomically fence the source, checkpoint and recorded blocked state.

        This UPDATE owns the row lock and revision comparison. A blocked
        source can only be cleared by a checked repair, not by publication.
        """
        expected_last = self.basis.last_block.number if self.basis.last_block else None
        result = await self.connection.execute(
            "UPDATE conix.source_progress "
            "SET last_block = %s, generation = %s, revision = revision + 1, "
            "blocked_reason = NULL "
            "WHERE singleton = 1 AND source_id = %s AND generation = %s "
            "AND revision = %s AND last_block IS NOT DISTINCT FROM %s "
            "AND blocked_reason IS NOT DISTINCT FROM %s",
            (
                last_number,
                generation,
                self.basis.source_id,
                self.basis.generation,
                self.basis.revision,
                expected_last,
                self.basis.blocked_reason,
            ),
        )
        if result.rowcount != 1:
            raise HistoryConflict("Durable checkpoint changed before publication.")

    async def publish_block(self, *, block: Block, events: tuple[RawEvent, ...]) -> Progress:
        """Commit one ALREADY validated complete block, including an empty one.

        The future scanner must establish full selected-event coverage before
        invoking this method. The journal's responsibility is durable fencing
        and atomic storage, not an independent RPC evidence algorithm.
        """
        if self.basis.blocked_reason is not None:
            raise HistoryConflict("Retained history is blocked; explicit repair is required.")

        previous = self.basis.last_block
        expected_number = previous.number + 1 if previous else self.basis.history_start

        if block.number != expected_number:
            raise HistoryConflict(f"{block.number = }, {expected_number = }")
        if previous is not None and block.parent_hash != previous.hash:
            raise HistoryConflict("Next block does not extend the retained checkpoint.")

        async with self._transaction():
            await self.connection.execute(
                "INSERT INTO conix.blocks (number, hash, parent_hash, timestamp) "
                "VALUES (%s, %s, %s, %s)",
                (block.number, block.hash, block.parent_hash, block.timestamp),
            )

            for event in events:
                if not 1 <= len(event.topics) <= 4:
                    raise ValueError("Raw event must contain between one and four topic words.")

                (topic0, topic1, topic2, topic3) = event.topics + (None,) * (4 - len(event.topics))
                await self.connection.execute(
                    "INSERT INTO conix.events ("
                    "block_number, log_index, transaction_hash, transaction_index, "
                    "address, topic0, topic1, topic2, topic3, data"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        block.number,
                        event.log_index,
                        event.transaction_hash,
                        event.transaction_index,
                        event.address,
                        topic0,
                        topic1,
                        topic2,
                        topic3,
                        event.data,
                    ),
                )

            await self._advance_progress(
                last_number=block.number,
                generation=self.basis.generation,
            )

        self.basis = replace(
            self.basis,
            last_block=block,
            revision=self.basis.revision + 1,
            blocked_reason=None,
        )
        return self.basis

    async def repair_to(self, *, ancestor: Block) -> Progress:
        """Retire a suffix after the caller independently identifies its ancestor.

        Block number and hash identify the retained ancestor. Other header
        fields come from the journal's own row, not the caller's copy.
        Establishing its compatibility with the current chain belongs to
        the core; no RPC executes inside this SQL transaction.
        """
        last = self.basis.last_block
        if last is None or not self.basis.history_start <= ancestor.number <= last.number:
            raise HistoryConflict("Repair ancestor is outside the retained indexed history.")

        new_generation = uuid4()
        async with self._transaction():
            row = await (
                await self.connection.execute(
                    "SELECT number, hash, parent_hash, timestamp "
                    "FROM conix.blocks WHERE number = %s",
                    (ancestor.number,),
                )
            ).fetchone()
            if row is None or row[1] != ancestor.hash:
                raise HistoryConflict("Repair ancestor does not match a retained block identity.")

            retained_ancestor = Block(*row)

            await self.connection.execute(
                "DELETE FROM conix.blocks WHERE number > %s",
                (ancestor.number,),
            )
            await self._advance_progress(
                last_number=ancestor.number,
                generation=new_generation,
            )

        self.basis = replace(
            self.basis,
            last_block=retained_ancestor,
            generation=new_generation,
            revision=self.basis.revision + 1,
            blocked_reason=None,
        )
        return self.basis

    async def close(self) -> None:
        """Releasing the connection also releases its session advisory lock."""
        self.retired = True
        await self.connection.close()


@asynccontextmanager
async def open_journal(*, catalog: Catalog, database_dsn: str):
    """Own the exclusive writer connection for one explicit session."""
    journal = await Journal.open(catalog=catalog, database_dsn=database_dsn)
    try:
        yield journal
    finally:
        await journal.close()


async def inspect_retained_ancestry(
    *, catalog: Catalog, database_dsn: str, limit: int, through: int | None = None,
) -> RetainedAncestry:
    """Expose stored ancestry without handing callers a writable journal.

    Access uses the same source checks, runtime identity and exclusive writer
    lock as the ordinary Tier-0 publisher. A supervisor may compare these
    facts with independently observed RPC headers but cannot repair here.
    """
    async with open_journal(catalog=catalog, database_dsn=database_dsn) as journal:
        return await journal.retained_ancestry(limit=limit, through=through)
