"""Read exact, checkpoint-bound event history without mutating the journal.

A reader observes one complete retained prefix under a PostgreSQL snapshot.
Pages filter original topic words in SQL and decode the selected ABI only
after selecting rows. A cursor binds that query to an exact checkpoint,
not to the current moving tip; appends preserve older pages, while manual
repair changes generation and invalidates them.

Historical calls are independent of log availability: they use EIP-1898
blockHash selectors and never fall back to a block number or latest state.
Canonical evidence reports what the explicitly trusted RPC provider says,
not a consensus proof or an implicit finality guarantee.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import time
from uuid import UUID

import httpx
import psycopg
from psycopg import AsyncConnection
from eth_utils import to_checksum_address

from conix.abi import (
    EventDescriptor,
    FunctionDescriptor,
    InvalidCallerValue,
    MalformedABIData,
    ResourceExceeded,
    decode_event,
    decode_return,
    encode_call,
    encode_indexed_filter,
)
from conix.catalog import Catalog, Contract, _rpc_url
from conix.core import BranchConflict, _block, _bytes, _quantity, verify_source
from conix.journal import Block, StorageUnavailable
from conix.rpc import RPC, RpcProtocolError, RpcUnavailable


class HistoryUnavailable(RuntimeError):
    """The selected durable history cannot provide the requested complete prefix."""


class CursorInvalid(ValueError):
    """The supplied checkpoint or continuation token no longer identifies a page."""


class HistoricalCallUnavailable(RuntimeError):
    """The requested function has no usable state or result at the selected block."""


Cursor = str
TOPIC_COLUMNS = {1: "e.topic1", 2: "e.topic2", 3: "e.topic3"}
CURSOR_LIMIT = 512
MAX_PAGE_RAW_BYTES = 8 * 1024 * 1024
CURSOR_CHARS = re.compile(r"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    source_id: bytes
    generation: UUID
    history_start: int
    block: Block
    revision: int


@dataclass(frozen=True, slots=True)
class Event:
    contract: str
    event: str
    address: str
    block: Block
    transaction_hash: bytes
    transaction_index: int
    log_index: int
    topics: tuple[bytes, ...]
    data: bytes
    values: tuple


@dataclass(frozen=True, slots=True)
class EventPage:
    checkpoint: Checkpoint
    history_start: int
    events: tuple[Event, ...]
    next_cursor: Cursor | None
    complete: bool


@dataclass(frozen=True, slots=True)
class CanonicalEvidence:
    checkpoint: Checkpoint
    blocks: tuple[Block, ...]
    finalized: Block | None
    finalized_status: str


def _require_checkpoint(checkpoint: object) -> Checkpoint:
    """Admit an externally supplied handle before reading its fields.

    The dataclass is immutable but callers may construct one with arbitrary
    member types; only the small identity-bearing subset is needed here.
    Its actual source/generation is checked against PostgreSQL separately.
    """
    if (
        type(checkpoint) is not Checkpoint
        or type(checkpoint.block) is not Block
        or type(checkpoint.source_id) is not bytes
        or len(checkpoint.source_id) != 32
        or type(checkpoint.generation) is not UUID
        or type(checkpoint.history_start) is not int
        or type(checkpoint.revision) is not int
        or type(checkpoint.block.number) is not int
        or type(checkpoint.block.hash) is not bytes
        or len(checkpoint.block.hash) != 32
        or not 0 <= checkpoint.history_start <= checkpoint.block.number
        or checkpoint.revision < 0
    ):
        raise CursorInvalid("Expected a valid immutable Conix checkpoint.")

    return checkpoint


@dataclass(frozen=True, slots=True)
class _State:
    source_id: bytes
    history_start: int
    generation: UUID
    revision: int
    tip_number: int | None
    selected_block: Block | None


def _cursor_binding(
    checkpoint: Checkpoint,
    contract: Contract,
    descriptor: EventDescriptor,
    filters: tuple[tuple[int, int, bytes], ...],
) -> str:
    """Bind an opaque continuation to source, query and exact old checkpoint."""
    selection = {
        "source": checkpoint.source_id.hex(),
        "generation": str(checkpoint.generation),
        "history_start": checkpoint.history_start,
        "checkpoint": [checkpoint.block.number, checkpoint.block.hash.hex(), checkpoint.revision],
        "contract": contract.alias,
        "address": contract.address.hex(),
        "event": descriptor.signature,
        "filters": [[ordinal, topic.hex()] for (ordinal, _, topic) in filters],
    }
    canonical = json.dumps(selection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(canonical).hexdigest()


def _encode_cursor(binding: str, position: tuple[int, int, int]) -> Cursor:
    payload = json.dumps(
        {"v": 1, "b": binding, "p": position},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Cursor, binding: str, checkpoint: Checkpoint) -> tuple[int, int, int]:
    """Reject mismatched/oversized cursors before constructing any SQL."""
    if (
        type(cursor) is not str
        or not 0 < len(cursor) <= CURSOR_LIMIT
        or CURSOR_CHARS.fullmatch(cursor) is None
    ):
        raise CursorInvalid("Continuation cursor is not a bounded opaque token.")

    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        document = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise CursorInvalid("Continuation cursor is not valid encoding.") from error

    if (
        type(document) is not dict
        or set(document) != {"v", "b", "p"}
        or type(document["v"]) is not int
        or document["v"] != 1
        or document["b"] != binding
        or type(document["p"]) is not list
        or len(document["p"]) != 3
        or any(type(number) is not int or number < 0 or number >= 2**63 for number in document["p"])
        or not checkpoint.history_start <= document["p"][0] <= checkpoint.block.number
        or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != cursor
    ):
        raise CursorInvalid("Continuation cursor does not match its checkpoint and query.")

    return tuple(document["p"])


class Reader:
    """Use a SELECT-only connection; never modify a journal or start RPC work in SQL."""

    def __init__(self, *, catalog: Catalog, connection: AsyncConnection, rpc: RPC):
        self.catalog = catalog
        self.connection = connection
        self.rpc = rpc
        # One PostgreSQL session cannot host overlapping read snapshots.
        # RPC calls need no such lock; independent readers may run in parallel.
        self._db_lock = asyncio.Lock()

    async def _state(self, block_number: int | None = None) -> _State:
        """Inspect source identity and one block under the caller's SQL snapshot."""
        try:
            row = await (
                await self.connection.execute(
                    "SELECT p.source_id, p.chain_id, p.genesis_hash, p.history_start, "
                    "p.generation, p.revision, p.last_block, p.blocked_reason, "
                    "b.number, b.hash, b.parent_hash, b.timestamp "
                    "FROM conix.source_progress p "
                    "LEFT JOIN conix.blocks b ON b.number = COALESCE(%s, p.last_block) "
                    "WHERE p.singleton = 1",
                    (block_number,),
                )
            ).fetchone()
        except psycopg.Error as error:
            raise StorageUnavailable("Reader could not inspect the durable history.") from error

        if row is None:
            raise HistoryUnavailable("Selected database has no initialized history.")
        if (
            row[0] != self.catalog.source_id
            or row[1] != self.catalog.chain.chain_id
            or row[2] != self.catalog.chain.genesis_hash
            or row[3] != self.catalog.chain.start_block
        ):
            raise HistoryUnavailable("Durable history belongs to a different selected source.")
        if row[7] is not None:
            raise HistoryUnavailable("The selected complete prefix is blocked pending repair.")

        block = None if row[8] is None else Block(*row[8:12])
        if block_number is None and row[6] is not None and block is None:
            raise HistoryUnavailable("Durable checkpoint has no stored block header.")

        return _State(
            source_id=row[0],
            history_start=row[3],
            generation=row[4],
            revision=row[5],
            tip_number=row[6],
            selected_block=block,
        )

    @staticmethod
    def _validate_checkpoint(checkpoint: Checkpoint, state: _State) -> None:
        """An append preserves a prefix; any repair changes its generation."""
        if (
            checkpoint.source_id != state.source_id
            or checkpoint.generation != state.generation
            or checkpoint.history_start != state.history_start
            or not 0 <= checkpoint.revision <= state.revision
            or state.tip_number is None
            or checkpoint.block.number > state.tip_number
            or checkpoint.block != state.selected_block
        ):
            raise CursorInvalid("Checkpoint was invalidated or does not belong to this retained prefix.")

    async def _check_live(self, checkpoint: Checkpoint) -> None:
        async with self._db_lock:
            state = await self._state(checkpoint.block.number)
            self._validate_checkpoint(checkpoint, state)

    @asynccontextmanager
    async def _snapshot(self, checkpoint: Checkpoint | None = None):
        """Use an admitted checkpoint inside one snapshot, then recheck it."""
        try:
            async with self._db_lock:
                async with self.connection.transaction():
                    await self.connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    state = await self._state(
                        None if checkpoint is None else checkpoint.block.number
                    )
                    if checkpoint is not None:
                        self._validate_checkpoint(checkpoint, state)
                    yield state
        except psycopg.Error as error:
            raise StorageUnavailable("Reader snapshot failed before completion.") from error

        # A reorg/repair after the snapshot would be invisible INSIDE it.
        # This new observation invalidates any result produced meanwhile.
        if checkpoint is not None:
            await self._check_live(checkpoint)

    async def checkpoint(self) -> Checkpoint | None:
        """Return the last completely published block; None means no prefix yet."""
        async with self._snapshot() as state:
            if state.tip_number is None:
                result = None
            else:
                result = Checkpoint(
                    source_id=state.source_id,
                    generation=state.generation,
                    history_start=state.history_start,
                    block=state.selected_block,
                    revision=state.revision,
                )

        if result is not None:
            await self._check_live(result)
        return result

    def _event(self, alias: str, signature: str) -> tuple[Contract, EventDescriptor]:
        try:
            contract = self.catalog.contract(alias)
        except KeyError:
            raise InvalidCallerValue("Selected contract alias is not in this catalog.") from None

        for selected in contract.descriptor.events:
            if selected.signature == signature:
                return contract, selected
        raise InvalidCallerValue("Selected event signature is not in this contract's catalog.")

    @staticmethod
    def _function(contract: Contract, signature: str) -> FunctionDescriptor:
        for selected in contract.descriptor.functions:
            if selected.signature == signature:
                return selected
        raise InvalidCallerValue("Function is not a declared view/pure ABI signature.")

    def _filters(
        self,
        descriptor: EventDescriptor,
        where: Mapping[str, object] | None,
    ) -> tuple[tuple[int, int, bytes], ...]:
        if where is None:
            return ()
        if not isinstance(where, Mapping):
            raise InvalidCallerValue("Event predicates must be an indexed-field mapping.")

        selected = []
        for (field, value) in where.items():
            topic = encode_indexed_filter(descriptor, field, value, limits=self.catalog.limits.abi)
            argument = next(argument for argument in descriptor.arguments if argument.name == field)
            selected.append((argument.ordinal, argument.topic_index, topic))
        return tuple(sorted(selected))

    async def events(
        self,
        *,
        checkpoint: Checkpoint,
        contract: str,
        event: str,
        where: Mapping[str, object] | None = None,
        cursor: Cursor | None = None,
        limit: int = 100,
    ) -> EventPage:
        """Read a keyset page up through this checkpoint, not the moving tip."""
        if type(limit) is not int or not 1 <= limit <= self.catalog.limits.max_page_size:
            raise ResourceExceeded("Event page size is outside its configured bounds.")
        _require_checkpoint(checkpoint)

        (selected, descriptor) = self._event(contract, event)
        filters = self._filters(descriptor, where)
        binding = _cursor_binding(checkpoint, selected, descriptor, filters)
        after = None if cursor is None else _decode_cursor(cursor, binding, checkpoint)

        # Topic columns come only from the compiled event's finite topic
        # positions, never from a user-provided field name or SQL expression.
        predicates = [
            "e.address = %s",
            "e.topic0 = %s",
            "e.block_number >= %s",
            "e.block_number <= %s",
        ]
        parameters = [
            selected.address,
            descriptor.topic0,
            checkpoint.history_start,
            checkpoint.block.number,
        ]
        for (_, topic_index, topic) in filters:
            predicates.append(f"{TOPIC_COLUMNS[topic_index]} = %s")
            parameters.append(topic)
        if after is not None:
            predicates.append("(e.block_number, e.transaction_index, e.log_index) > (%s, %s, %s)")
            parameters.extend(after)

        statement = (
            "SELECT b.number, b.hash, b.parent_hash, b.timestamp, "
            "e.transaction_hash, e.transaction_index, e.log_index, e.address, "
            "e.topic0, e.topic1, e.topic2, e.topic3, e.data "
            "FROM conix.events e JOIN conix.blocks b ON b.number = e.block_number "
            "WHERE " + " AND ".join(predicates) + " "
            "ORDER BY e.block_number, e.transaction_index, e.log_index LIMIT %s"
        )
        parameters.append(limit + 1)

        rows = []
        raw_bytes = 0
        has_more = False

        # A LIMIT bounds rows, not their bytes. A server-side cursor keeps
        # large BYTEA values out of an unbounded client-side fetchall().
        # Stop at the last fully accepted event, preserving keyset order.
        async with self._snapshot(checkpoint):
            async with self.connection.cursor(name="conix_event_page") as result:
                result.itersize = 8
                await result.execute(statement, parameters)

                async for row in result:
                    if len(rows) == limit:
                        has_more = True
                        break

                    row_bytes = (
                        52
                        + 32 * sum(topic is not None for topic in row[8:12])
                        + len(row[12])
                    )
                    if raw_bytes + row_bytes > MAX_PAGE_RAW_BYTES:
                        if not rows:
                            raise ResourceExceeded("One retained event exceeds the page-byte budget.")
                        has_more = True
                        break

                    rows.append(row)
                    raw_bytes += row_bytes

            # Decode the selected ABI while the same snapshot is held. The
            # post-snapshot generation check must happen AFTER the page has
            # been constructed, not before potentially expensive decoding.
            complete = not has_more
            events = []
            for row in rows:
                block = Block(*row[:4])
                topics = tuple(topic for topic in row[8:12] if topic is not None)
                data = row[12]
                try:
                    values = decode_event(descriptor, topics, data, limits=self.catalog.limits.abi)
                except MalformedABIData as error:
                    raise HistoryUnavailable("The retained event bytes contradict their selected ABI.") from error

                events.append(Event(
                    contract=selected.alias,
                    event=descriptor.signature,
                    address=to_checksum_address(row[7]),
                    block=block,
                    transaction_hash=row[4],
                    transaction_index=row[5],
                    log_index=row[6],
                    topics=topics,
                    data=data,
                    values=values,
                ))

        next_cursor = None
        if not complete:
            last = events[-1]
            next_cursor = _encode_cursor(
                binding,
                (last.block.number, last.transaction_index, last.log_index),
            )

        return EventPage(
            checkpoint=checkpoint,
            history_start=checkpoint.history_start,
            events=tuple(events),
            next_cursor=next_cursor,
            complete=complete,
        )

    async def call(
        self,
        *,
        checkpoint: Checkpoint,
        contract: str,
        function: str,
        arguments: tuple,
    ) -> tuple:
        """Execute exactly one read-only function at a canonical block hash."""
        _require_checkpoint(checkpoint)

        try:
            selected = self.catalog.contract(contract)
        except KeyError:
            raise InvalidCallerValue("Selected contract alias is not in this catalog.") from None

        descriptor = self._function(selected, function)
        encoded = encode_call(descriptor, arguments, limits=self.catalog.limits.abi)
        block = {"blockHash": "0x" + checkpoint.block.hash.hex(), "requireCanonical": True}
        address = to_checksum_address(selected.address)
        deadline = time.monotonic() + self.catalog.limits.operation_timeout_seconds

        await self._check_live(checkpoint)
        try:
            code = _bytes(
                await self.rpc.call("eth_getCode", [address, block], deadline=deadline),
                None,
            )
            if not code:
                raise HistoricalCallUnavailable("No contract code exists at the selected checkpoint.")

            result = await self.rpc.call(
                "eth_call",
                [{"to": address, "data": "0x" + encoded.hex()}, block],
                deadline=deadline,
            )
            decoded = decode_return(
                descriptor,
                _bytes(result, None),
                limits=self.catalog.limits.abi,
            )
        except (RpcUnavailable, RpcProtocolError, MalformedABIData) as error:
            raise HistoricalCallUnavailable(
                "Pinned historical call is unavailable, reverted or noncanonical."
            ) from error

        # requireCanonical applies when the RPC is executed. A reorg may
        # happen immediately afterward without changing the SQL journal.
        if await self._observed(checkpoint.block.number, deadline) != checkpoint.block:
            raise BranchConflict("Historical call checkpoint became noncanonical.")
        await self._check_live(checkpoint)
        return decoded

    async def _observed(self, number: int, deadline: float) -> Block:
        response = await self.rpc.call(
            "eth_getBlockByNumber",
            [hex(number), False],
            deadline=deadline,
        )
        if response is None:
            raise HistoryUnavailable("Provider cannot identify a requested canonical block.")
        return _block(response, number).header

    async def evidence(
        self,
        *,
        checkpoint: Checkpoint,
        block_numbers: tuple[int, ...],
        include_finalized: bool = True,
    ) -> CanonicalEvidence:
        """Match stored ancestors against canonical RPC headers; report finality only as provider evidence."""
        _require_checkpoint(checkpoint)
        if (
            type(block_numbers) is not tuple
            or len(block_numbers) > self.catalog.limits.max_evidence_blocks
            or any(
                type(number) is not int
                or not checkpoint.history_start <= number <= checkpoint.block.number
                for number in block_numbers
            )
            or len(set(block_numbers)) != len(block_numbers)
            or type(include_finalized) is not bool
        ):
            raise InvalidCallerValue(
                "Ancestry numbers must identify unique retained blocks within the evidence cap."
            )

        async with self._snapshot(checkpoint):
            result = await self.connection.execute(
                "SELECT number, hash, parent_hash, timestamp FROM conix.blocks "
                "WHERE number = ANY(%s)",
                (list(block_numbers),),
            )
            stored = {row[0]: Block(*row) for row in await result.fetchall()}

        if len(stored) != len(block_numbers):
            raise HistoryUnavailable("Retained history has missing ancestry blocks.")

        deadline = time.monotonic() + self.catalog.limits.operation_timeout_seconds
        if await self._observed(checkpoint.block.number, deadline) != checkpoint.block:
            raise BranchConflict("Retained checkpoint is not canonical at the provider.")

        for number in block_numbers:
            if await self._observed(number, deadline) != stored[number]:
                raise BranchConflict("Requested ancestry block differs from the provider's canonical history.")

        finalized = None
        finalized_status = "unknown"
        if include_finalized:
            try:
                response = await self.rpc.call(
                    "eth_getBlockByNumber",
                    ["finalized", False],
                    deadline=deadline,
                )
            except RpcUnavailable:
                response = None

            if response is not None:
                if type(response) is not dict:
                    raise HistoryUnavailable("Provider returned malformed finalized block evidence.")

                number = _quantity(response.get("number"))
                reported = _block(response, number).header
                if await self._observed(number, deadline) != reported:
                    raise BranchConflict("Provider finalized block differs from its canonical chain.")

                # A provider can consistently report the WRONG older
                # canonical block. When that height lies in our complete
                # prefix, the reported finalized ancestor must also match
                # the retained history, not just another provider reply.
                if checkpoint.history_start <= number < checkpoint.block.number:
                    async with self._snapshot(checkpoint):
                        row = await (
                            await self.connection.execute(
                                "SELECT hash FROM conix.blocks WHERE number = %s",
                                (number,),
                            )
                        ).fetchone()
                        if row is None or row[0] != reported.hash:
                            raise BranchConflict("Reported finality contradicts the retained prefix.")

                finalized = reported
                finalized_status = "known"

        # This second canonical read detects a branch change while collecting
        # evidence; the SQL check separately catches repair/generation changes.
        if await self._observed(checkpoint.block.number, deadline) != checkpoint.block:
            raise BranchConflict("Retained checkpoint changed during ancestry observation.")
        await self._check_live(checkpoint)

        return CanonicalEvidence(
            checkpoint=checkpoint,
            blocks=tuple(stored[number] for number in block_numbers),
            finalized=finalized,
            finalized_status=finalized_status,
        )


@asynccontextmanager
async def open_reader(*, catalog: Catalog, database_dsn: str, rpc_url: str):
    """Own a SELECT-only PostgreSQL connection and selected, checked RPC session."""
    selected_url = _rpc_url(rpc_url)
    try:
        connection = await AsyncConnection.connect(
            database_dsn,
            connect_timeout=5,
            autocommit=True,
        )
    except psycopg.Error as error:
        raise StorageUnavailable("Cannot open the selected read-only PostgreSQL connection.") from error

    async with connection:
        try:
            await connection.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(int(catalog.limits.operation_timeout_seconds * 1000)),),
            )
            identity = await (
                await connection.execute("SELECT current_user")
            ).fetchone()
        except psycopg.Error as error:
            raise StorageUnavailable("Cannot inspect the selected reader database role.") from error

        if identity != ("conix_reader",):
            raise HistoryUnavailable("Reader requires the dedicated SELECT-only PostgreSQL role.")

        async with httpx.AsyncClient(trust_env=False, timeout=None) as client:
            rpc = RPC(client=client, url=selected_url, limits=catalog.limits)
            await verify_source(catalog=catalog, rpc=rpc)
            reader = Reader(catalog=catalog, connection=connection, rpc=rpc)

            async with reader._snapshot():
                pass  # Require the selected durable source, even before any block exists.
            yield reader
