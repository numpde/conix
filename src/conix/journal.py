"""The only owner of durable history and progress mutations.

A Writer retains the PostgreSQL connection that holds its advisory lock. Every
mutation uses that connection; no extension gets raw write access. RPC work must
happen outside transactions. All history is rebuildable; no financial records
are stored here.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from uuid import UUID, uuid4

from eth_utils import to_checksum_address
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from conix.configuration import Settings
from conix.errors import Blocked, BranchConflict, ConfigurationError, CorruptStore, StateChanged, WriterBusy
from conix.models import Block, Checkpoint, Collection, CollectionCursor, CollectionPage, HistoryUnavailable

VERSION = 1
ABI_PROFILE = "PayToPlay/2|asset()|viewPosition(address,uint256,address)|VoucherCollected(address,uint256,address,uint256,uint256,bytes32)"


def source_digest(settings: Settings) -> bytes:
    document = {
        "chain_id": settings.deployment.chain_id,
        "contract": settings.deployment.address.lower(),
        "start_block": settings.start_block,
        "abi": ABI_PROFILE,
    }
    return sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).digest()


def advisory_key(settings: Settings) -> int:
    # Lock same source within one PostgreSQL database, independently of schema version.
    return int.from_bytes(source_digest(settings)[:8], "big", signed=True)


@dataclass(frozen=True, slots=True)
class Basis:
    generation: UUID
    revision: int
    checkpoint: Block | None
    collection_count: int
    blocked_reason: str | None
    upstream_head: Block | None
    head_observed_at: datetime | None


def _block(row) -> Block:
    return Block(int(row["number"]), bytes(row["hash"]), bytes(row["parent_hash"]), int(row["timestamp"]))


async def _state(connection: AsyncConnection, settings: Settings) -> Basis:
    row = await (await connection.execute("SELECT * FROM conix.source_progress WHERE id = 1")).fetchone()
    if row is None:
        raise ConfigurationError("conix schema is not initialized. Run conix init.")
    if (
        row["schema_version"] != VERSION
        or bytes(row["source_digest"]) != source_digest(settings)
        or int(row["chain_id"]) != settings.deployment.chain_id
        or bytes(row["contract_address"]) != bytes.fromhex(settings.deployment.address[2:])
        or int(row["start_block"]) != settings.start_block
    ):
        raise ConfigurationError(
            "Stored conix schema/source identity differs from configuration; select the original source or migrate explicitly."
        )
    checkpoint = None
    if row["completed_height"] is not None:
        block_row = await (await connection.execute(
            "SELECT * FROM conix.blocks WHERE number = %s", (row["completed_height"],)
        )).fetchone()
        if block_row is None or bytes(block_row["hash"]) != bytes(row["completed_hash"]):
            raise CorruptStore("Completed checkpoint has no matching stored block.")
        checkpoint = _block(block_row)
    elif int(row["collection_count"]) != 0:
        raise CorruptStore("Unstarted source has a nonzero collection count.")
    head = None
    if row["head_height"] is not None:
        head = Block(int(row["head_height"]), bytes(row["head_hash"]),
                     bytes(row["head_parent_hash"]), int(row["head_timestamp"]))
    return Basis(
        row["generation"], int(row["revision"]), checkpoint,
        int(row["collection_count"]), row["blocked_reason"],
        head, row["head_observed_at"],
    )


async def _check_basis(connection: AsyncConnection, settings: Settings, expected: Basis) -> Basis:
    current = await _state(connection, settings)
    if (current.generation, current.revision, current.checkpoint, current.collection_count) != (
        expected.generation, expected.revision, expected.checkpoint, expected.collection_count
    ):
        raise StateChanged("The source publication basis changed; discard prepared work and reread progress.")
    return current


def _address(value: str) -> bytes:
    return bytes.fromhex(value[2:])


def _collection(row) -> Collection:
    return Collection(
        int(row["block_number"]), bytes(row["block_hash"]), int(row["block_timestamp"]),
        bytes(row["transaction_hash"]), int(row["transaction_index"]), int(row["log_index"]),
        to_checksum_address(bytes(row["customer"])), int(row["channel_id"]),
        to_checksum_address(bytes(row["payee"])), int(row["amount"]),
        int(row["voucher_paid"]), bytes(row["invoice_hash"]),
    )


class Journal:
    """Bounded, one-source storage interface; mutations require an owned Writer."""

    def __init__(self, dsn: str, settings: Settings):
        self._dsn = dsn
        self.settings = settings

    @asynccontextmanager
    async def _connection(self):
        async with await AsyncConnection.connect(
            self._dsn, autocommit=True, row_factory=dict_row,
            connect_timeout=10,
        ) as connection:
            # PostgreSQL enforces these deadlines independently of Python
            # cancellation or an unreachable database/client connection.
            milliseconds = max(1, int(self.settings.operation_timeout_seconds * 1000))
            await connection.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{milliseconds}ms",),
            )
            await connection.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{min(milliseconds, 5000)}ms",),
            )
            role = await (await connection.execute(
                "SELECT r.rolname, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
                "r.rolreplication, r.rolbypassrls, "
                "pg_has_role(current_user, pg_get_userbyid(n.nspowner), 'member') AS owner_member, "
                "has_schema_privilege(current_user, 'conix', 'CREATE') AS schema_create "
                "FROM pg_roles r JOIN pg_namespace n ON n.nspname = 'conix' "
                "WHERE r.rolname = current_user"
            )).fetchone()
            if (
                role is None
                or role["rolname"] != self.settings.runtime_role
                or role["owner_member"] or role["schema_create"]
                or any(role[name] for name in (
                    "rolsuper", "rolcreatedb", "rolcreaterole",
                    "rolreplication", "rolbypassrls"
                ))
            ):
                raise ConfigurationError(
                    "Conix requires its selected restricted runtime login, not a schema owner "
                    "or an account with cluster/schema creation privileges."
                )
            yield connection

    @asynccontextmanager
    async def _snapshot(self):
        async with self._connection() as connection:
            async with connection.transaction():
                await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                yield connection

    @asynccontextmanager
    async def writer(self):
        """Hold the same PostgreSQL session lock for the lifetime of all writes."""
        async with self._connection() as connection:
            key = advisory_key(self.settings)
            row = await (await connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,))).fetchone()
            if not row["acquired"]:
                raise WriterBusy("Another conix writer or repair operation owns this source.")
            writer = Writer(self, connection)
            try:
                yield writer
            finally:
                writer._active = False
                # Connection close also releases the advisory lock after failure.
                if not connection.closed:
                    await connection.execute("SELECT pg_advisory_unlock(%s)", (key,))

    async def basis(self) -> Basis:
        async with self._snapshot() as connection:
            return await _state(connection, self.settings)

    async def ensure_checkpoint(self, checkpoint: Checkpoint) -> Basis:
        if checkpoint.deployment != self.settings.deployment:
            raise HistoryUnavailable("Checkpoint names another source.")
        async with self._snapshot() as connection:
            state = await _state(connection, self.settings)
            if state.blocked_reason or state.generation != checkpoint.generation:
                raise HistoryUnavailable("History is blocked or belongs to a retired generation.")
            if state.checkpoint is None or checkpoint.block.number > state.checkpoint.number:
                raise HistoryUnavailable("Checkpoint is ahead of completed history.")
            row = await (await connection.execute(
                "SELECT * FROM conix.blocks WHERE number = %s", (checkpoint.block.number,)
            )).fetchone()
            if row is None or _block(row) != checkpoint.block:
                raise HistoryUnavailable("Checkpoint block was replaced or is not retained.")
            count = await (await connection.execute(
                "SELECT COUNT(*) AS n FROM conix.collections WHERE block_number <= %s",
                (checkpoint.block.number,),
            )).fetchone()
            if int(count["n"]) != checkpoint.collection_count:
                raise HistoryUnavailable("Checkpoint collection prefix no longer agrees with retained history.")
            return state

    async def page(self, checkpoint: Checkpoint, cursor: CollectionCursor | None, limit: int) -> CollectionPage:
        if type(limit) is not int or not 1 <= limit <= self.settings.max_page_size:
            raise ValueError("Collection page limit exceeds the configured bound.")
        if type(checkpoint) is not Checkpoint or checkpoint.deployment != self.settings.deployment:
            raise HistoryUnavailable("Collection history requires a checkpoint from this source.")
        if cursor is not None and (
            type(cursor) is not CollectionCursor or cursor.generation != checkpoint.generation
            or type(cursor.next_sequence) is not int or cursor.next_sequence < 0
            or cursor.next_sequence > checkpoint.collection_count
        ):
            raise HistoryUnavailable("Collection cursor is foreign, ahead or invalid; resynchronize.")
        offset = 0 if cursor is None else cursor.next_sequence
        async with self._snapshot() as connection:
            state = await _state(connection, self.settings)
            if state.blocked_reason or state.generation != checkpoint.generation:
                raise HistoryUnavailable("Collection generation was invalidated; resynchronize.")
            if state.checkpoint is None or checkpoint.block.number > state.checkpoint.number:
                raise HistoryUnavailable("Requested checkpoint has not been published.")
            block_row = await (await connection.execute(
                "SELECT * FROM conix.blocks WHERE number = %s", (checkpoint.block.number,)
            )).fetchone()
            if block_row is None or _block(block_row) != checkpoint.block:
                raise HistoryUnavailable("Collection checkpoint block was replaced.")
            first = await (await connection.execute(
                "SELECT * FROM conix.blocks WHERE number = %s", (self.settings.start_block,)
            )).fetchone()
            if first is None:
                raise CorruptStore("Retained history has no declared opening block.")
            rows = await (await connection.execute(
                "SELECT * FROM conix.collections WHERE sequence >= %s AND sequence < %s "
                "ORDER BY sequence LIMIT %s",
                (offset, checkpoint.collection_count, limit),
            )).fetchall()
            next_offset = offset + len(rows)
            if [int(row["sequence"]) for row in rows] != list(range(offset, next_offset)):
                raise CorruptStore("Collection history has a missing or duplicate sequence.")
            if next_offset < checkpoint.collection_count and len(rows) < limit:
                raise CorruptStore("Collection page ended before its complete retained prefix.")
            return CollectionPage(
                checkpoint, _block(first), tuple(_collection(row) for row in rows),
                CollectionCursor(checkpoint.generation, next_offset),
                next_offset == checkpoint.collection_count,
            )

    async def headers_at(self, checkpoint: Checkpoint, numbers: tuple[int, ...]) -> tuple[Block, ...]:
        """Read selected retained headers from one snapshot of a completed chain prefix.

        The writer has already validated and published every intermediate parent
        link. A reader need not load millions of unchanged intermediate rows.
        """
        if (
            type(checkpoint) is not Checkpoint
            or checkpoint.deployment != self.settings.deployment
            or type(numbers) is not tuple
            or len(numbers) > self.settings.max_evidence_blocks
            or any(
                type(number) is not int
                or number < self.settings.start_block
                or number > checkpoint.block.number
                for number in numbers
            )
            or len(set(numbers)) != len(numbers)
        ):
            raise HistoryUnavailable("Selected headers must be distinct, retained and bounded.")
        async with self._snapshot() as connection:
            state = await _state(connection, self.settings)
            if (
                state.blocked_reason
                or state.generation != checkpoint.generation
                or state.checkpoint is None
                or state.checkpoint.number < checkpoint.block.number
            ):
                raise HistoryUnavailable("Requested chain generation/checkpoint is no longer usable.")
            rows = await (await connection.execute(
                "SELECT * FROM conix.blocks WHERE number = ANY(%s::bigint[])",
                (list(numbers),),
            )).fetchall()
            blocks = {int(row["number"]): _block(row) for row in rows}
            if len(blocks) != len(numbers):
                raise CorruptStore("Retained complete prefix is missing a requested block.")
            if blocks.get(checkpoint.block.number, checkpoint.block) != checkpoint.block:
                raise HistoryUnavailable("Completed checkpoint block was replaced.")
            return tuple(blocks[number] for number in numbers)

    async def stored_headers(self, first: int, last: int) -> tuple[Block, ...]:
        """Retrieve exact retained canonical ancestry, without upstream RPC."""
        if type(first) is not int or type(last) is not int or first > last:
            raise ValueError("Header range must have ordered integer bounds.")
        if last - first + 1 > self.settings.max_evidence_blocks:
            raise HistoryUnavailable("Requested header range exceeds max_evidence_blocks.")
        async with self._snapshot() as connection:
            state = await _state(connection, self.settings)
            if state.blocked_reason or state.checkpoint is None or first < self.settings.start_block or last > state.checkpoint.number:
                raise HistoryUnavailable("Requested ancestry is not covered by usable stored history.")
            rows = await (await connection.execute(
                "SELECT * FROM conix.blocks WHERE number BETWEEN %s AND %s ORDER BY number",
                (first, last),
            )).fetchall()
            blocks = tuple(map(_block, rows))
            if len(blocks) != last - first + 1 or any(
                b.number != a.number + 1 or b.parent_hash != a.hash for a, b in zip(blocks, blocks[1:])
            ):
                raise CorruptStore("Stored block ancestry has a gap or broken parent connection.")
            return blocks


class Writer:
    """Concrete writer capability; never transfer across sessions or connections."""

    def __init__(self, journal: Journal, connection: AsyncConnection):
        self._journal = journal
        self._connection = connection
        self._active = True

    def _require_active(self):
        if not self._active or self._connection.closed:
            raise StateChanged("Writer session is no longer live; acquire fresh ownership.")

    async def basis(self) -> Basis:
        self._require_active()
        return await _state(self._connection, self._journal.settings)

    async def note_head(self, expected: Basis, head: Block, now: datetime):
        self._require_active()
        async with self._connection.transaction():
            await _check_basis(self._connection, self._journal.settings, expected)
            await self._connection.execute(
                "UPDATE conix.source_progress SET head_height=%s, head_hash=%s, "
                "head_parent_hash=%s, head_timestamp=%s, head_observed_at=%s, revision=revision+1 WHERE id=1",
                (head.number, head.hash, head.parent_hash, head.timestamp, now),
            )

    async def publish_block(self, expected: Basis, block: Block,
                            collections: tuple[Collection, ...], head: Block, now: datetime):
        """The only forward publication; one complete block and cursor per transaction."""
        self._require_active()
        settings = self._journal.settings
        async with self._connection.transaction():
            current = await _check_basis(self._connection, settings, expected)
            if current.blocked_reason:
                raise Blocked("Indexed history is blocked; repair before publishing.")
            next_height = settings.start_block if current.checkpoint is None else current.checkpoint.number + 1
            if block.number != next_height or (
                current.checkpoint is not None and block.parent_hash != current.checkpoint.hash
            ):
                raise StateChanged("Publication does not extend the current completed block by exactly one.")
            if len({event.log_index for event in collections}) != len(collections):
                raise ValueError("Duplicate block-global log index in one block.")
            if tuple(sorted(collections, key=lambda e: (e.transaction_index, e.log_index))) != collections:
                raise ValueError("Collection event order must follow transaction/log order.")
            for event in collections:
                if (event.block_number, event.block_hash, event.block_timestamp) != (block.number, block.hash, block.timestamp):
                    raise ValueError("Collection event disagrees with its containing block.")
                if not (0 < event.amount <= event.voucher_paid < 2**256 and 0 <= event.channel_id < 2**256):
                    raise ValueError("Collection amount, voucher total or channel identifier exceeds uint256 bounds.")
            await self._connection.execute(
                "INSERT INTO conix.blocks(number,hash,parent_hash,timestamp) VALUES (%s,%s,%s,%s)",
                (block.number, block.hash, block.parent_hash, block.timestamp),
            )
            for index, event in enumerate(collections):
                await self._connection.execute(
                    "INSERT INTO conix.collections "
                    "(sequence,block_number,block_hash,block_timestamp,transaction_hash,transaction_index,log_index,"
                    "customer,channel_id,payee,amount,voucher_paid,invoice_hash) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (current.collection_count + index, event.block_number, event.block_hash,
                     event.block_timestamp, event.transaction_hash, event.transaction_index, event.log_index,
                     _address(event.customer), event.channel_id, _address(event.payee),
                     event.amount, event.voucher_paid, event.invoice_hash),
                )
            await self._connection.execute(
                "UPDATE conix.source_progress SET completed_height=%s, completed_hash=%s, "
                "collection_count=%s, head_height=%s, head_hash=%s, head_parent_hash=%s, "
                "head_timestamp=%s, head_observed_at=%s, revision=revision+1 WHERE id=1",
                (block.number, block.hash, current.collection_count + len(collections),
                 head.number, head.hash, head.parent_hash, head.timestamp, now),
            )

    async def invalidate(self, expected: Basis, reason: str):
        """Fail closed on a proven contradiction; never discard collection evidence here."""
        self._require_active()
        if not reason or len(reason) > 1000:
            raise ValueError("Invalidation requires a bounded actionable reason.")
        async with self._connection.transaction():
            await _check_basis(self._connection, self._journal.settings, expected)
            await self._connection.execute(
                "UPDATE conix.source_progress SET generation=%s, blocked_reason=%s, revision=revision+1 WHERE id=1",
                (uuid4(), reason),
            )

    async def repair_to(self, expected: Basis, target: Block | None, chain):
        """Sole repair mutation: validate canonical source, then atomically rewind."""
        self._require_active()
        settings = self._journal.settings
        # The controller may propose a target; it cannot certify one. RPC is
        # outside the SQL transaction, but checked under exclusive ownership.
        await chain.assert_source()
        if target is not None and not await chain.canonical(target):
            raise BranchConflict("The repair target is no longer canonical; choose a verified ancestor.")
        async with self._connection.transaction():
            current = await _check_basis(self._connection, settings, expected)
            if target is not None:
                if current.checkpoint is None or not settings.start_block <= target.number <= current.checkpoint.number:
                    raise StateChanged("Repair target is outside retained completed history.")
                row = await (await self._connection.execute(
                    "SELECT * FROM conix.blocks WHERE number=%s", (target.number,)
                )).fetchone()
                if row is None or _block(row) != target:
                    raise StateChanged("Repair target differs from stored block identity.")
                await self._connection.execute("DELETE FROM conix.blocks WHERE number > %s", (target.number,))
                count = int((await (await self._connection.execute(
                    "SELECT COUNT(*) AS n FROM conix.collections"
                )).fetchone())["n"])
                height, block_hash = target.number, target.hash
            else:
                await self._connection.execute("DELETE FROM conix.blocks")
                count, height, block_hash = 0, None, None
            await self._connection.execute(
                "UPDATE conix.source_progress SET generation=%s, revision=revision+1, "
                "completed_height=%s, completed_hash=%s, collection_count=%s, blocked_reason=NULL, "
                "head_height=NULL,head_hash=NULL,head_parent_hash=NULL,head_timestamp=NULL,head_observed_at=NULL "
                "WHERE id=1",
                (uuid4(), height, block_hash, count),
            )
