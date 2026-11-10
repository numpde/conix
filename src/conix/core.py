"""Establish and publish one block's selected EVM event history.

The offline catalog defines every (address, event topic0) selector. Scan ALL
selectors against the same block hash, admit their original logs using the
block's transaction inventory and selected ABI, then recheck the candidate
before the journal atomically advances the complete prefix. An empty result
counts only after every selected query succeeds.

Completeness is relative to the explicitly trusted RPC provider, not a
cryptographic proof of provider honesty or chain finality. This module owns
RPC evidence and canonicality checks; the journal alone owns SQL writes,
writer exclusion and unknown-COMMIT handling. No receipt, range, retry or
consumer-filtered acquisition path is implemented here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import re
import time

import httpx

from conix.abi import MalformedABIData, ResourceExceeded, decode_event
from conix.catalog import Catalog, _rpc_url
from conix.diagnostics import note_argument
from conix.journal import Block, Journal, Progress, RawEvent, open_journal
from conix.rpc import RPC, RpcProtocolError, RpcUnavailable


QUANTITY = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]*)\Z")
HEX = re.compile(r"0x(?:[0-9a-fA-F]{2})*\Z")
MAX_BLOCK_RAW_BYTES = 8 * 1024 * 1024


class BranchConflict(RuntimeError):
    """RPC observations contradict the retained or candidate canonical branch."""


@dataclass(frozen=True, slots=True)
class ObservedBlock:
    """One numbered block with its ordered transaction-hash inventory.

    Each selected log's transaction index must resolve to the claimed
    transaction hash here; this does not prove complete receipts.
    """

    header: Block
    transactions: tuple[bytes, ...]


def _quantity(value: object) -> int:
    if type(value) is not str or QUANTITY.fullmatch(value) is None:
        raise note_argument(RpcProtocolError("RPC value is not a canonical hex quantity."))

    number = int(value, 16)
    if number >= 2**63:
        raise note_argument(ResourceExceeded("RPC quantity exceeds the supported integer range."))
    return number


def _bytes(value: object, size: int | None) -> bytes:
    if type(value) is not str or HEX.fullmatch(value) is None:
        raise note_argument(RpcProtocolError("RPC value is not an even-length hexadecimal byte string."))

    decoded = bytes.fromhex(value[2:])
    if size is not None and len(decoded) != size:
        raise note_argument(
            RpcProtocolError(f"RPC value has {len(decoded)} bytes; expected {size}.")
        )
    return decoded


def _block(response: object, expected_number: int) -> ObservedBlock:
    """Admit the exact-height header and hash-only transaction inventory."""
    if type(response) is not dict:
        raise RpcProtocolError("RPC block header is not an object.")

    number = _quantity(response.get("number"))
    if number != expected_number:
        raise RpcProtocolError(f"{number = }, {expected_number = }")

    block = Block(
        number=number,
        hash=_bytes(response.get("hash"), 32),
        parent_hash=_bytes(response.get("parentHash"), 32),
        timestamp=_quantity(response.get("timestamp")),
    )
    transaction_hashes = response.get("transactions")
    if type(transaction_hashes) is not list:
        raise RpcProtocolError("RPC block lacks its transaction-hash inventory.")

    return ObservedBlock(
        header=block,
        transactions=tuple(
            _bytes(transaction_hash, 32)
            for transaction_hash in transaction_hashes
        ),
    )


def _event(
    response: object,
    *,
    observed: ObservedBlock,
    contract_address: bytes,
    descriptor,
    limits,
) -> RawEvent:
    """Check one selected log against its queried block, contract and ABI.

    The provider may return a malformed or incorrectly filtered response;
    matching its declared block, emitter and transaction inventory is not
    optional merely because the request used blockHash and topic0.
    """
    if type(response) is not dict or response.get("removed", False) is not False:
        raise RpcProtocolError("Selected RPC log is malformed or marked removed.")

    header = observed.header
    if (
        _bytes(response.get("blockHash"), 32) != header.hash
        or _quantity(response.get("blockNumber")) != header.number
        or _bytes(response.get("address"), 20) != contract_address
    ):
        raise RpcProtocolError("Selected RPC log belongs to a different block or contract.")

    transaction_index = _quantity(response.get("transactionIndex"))
    log_index = _quantity(response.get("logIndex"))
    transaction_hash = _bytes(response.get("transactionHash"), 32)

    if (
        transaction_index >= len(observed.transactions)
        or observed.transactions[transaction_index] != transaction_hash
    ):
        raise RpcProtocolError("Selected log contradicts the block transaction inventory.")

    # Decode to prove ABI admissibility, but persist the ORIGINAL topic/data
    # bytes. The journal is an event record, not a second ABI interpretation.
    declared_topics = response.get("topics")
    if type(declared_topics) is not list:
        raise RpcProtocolError("Selected RPC log does not contain a topic array.")

    topics = tuple(_bytes(topic, 32) for topic in declared_topics)
    data = _bytes(response.get("data"), None)

    try:
        decode_event(descriptor, topics, data, limits=limits.abi)
    except MalformedABIData as error:
        raise RpcProtocolError("Selected log violates its admitted ABI layout.") from error

    return RawEvent(
        transaction_hash=transaction_hash,
        transaction_index=transaction_index,
        log_index=log_index,
        address=contract_address,
        topics=topics,
        data=data,
    )


async def verify_source(*, catalog: Catalog, rpc: RPC) -> None:
    """Bind an RPC session to a selected chain ID AND genesis block.

    Chain IDs may be reused by unrelated networks. This is startup
    admission, not a substitute for checking canonicality per operation.
    """
    deadline = time.monotonic() + catalog.limits.operation_timeout_seconds
    chain_id = _quantity(
        (await rpc.call("eth_chainId", [], deadline=deadline)),
    )
    if chain_id != catalog.chain.chain_id:
        raise BranchConflict("Selected provider has a different EVM chain ID.")

    response = await rpc.call("eth_getBlockByNumber", ["0x0", False], deadline=deadline)
    genesis = None if response is None else _block(response, 0)
    if genesis is None or genesis.header.hash != catalog.chain.genesis_hash:
        raise BranchConflict("Selected provider has a different genesis block.")


class Writer:
    """Own the complete-evidence decision, not the SQL mutation.

    An arbitrary list passed to the journal cannot establish RPC coverage:
    every selected query and the branch recheck belong to advance_one().
    """

    def __init__(self, *, catalog: Catalog, journal: Journal, rpc: RPC):
        self.catalog = catalog
        self.journal = journal
        self.rpc = rpc

    async def _block(self, number: int, deadline: float) -> ObservedBlock | None:
        """Read the numbered block with transaction hashes (RPC flag False)."""
        response = await self.rpc.call(
            "eth_getBlockByNumber",
            [hex(number), False],
            deadline=deadline,
        )
        return None if response is None else _block(response, number)

    async def verify_source(self) -> None:
        """Bind the writer to the same chain identity used by readers."""
        await verify_source(catalog=self.catalog, rpc=self.rpc)

    async def advance_one(self) -> Progress | None:
        """Publish at most one fully observed block or return None at the tip.

        The retained tip must still be canonical. Every selected query must
        succeed and its logs must match the candidate's transactions and
        ABI. Recheck the block after acquisition; any failure publishes
        NOTHING, including a seemingly empty block. The journal alone
        performs the ensuing atomic SQL publication.
        """
        deadline = time.monotonic() + self.catalog.limits.operation_timeout_seconds
        previous = self.journal.basis.last_block

        # A reorg can replace the retained tip even when no new block exists.
        # Always re-observe that identity before advancing or reporting a tip.
        if previous is not None:
            retained = await self._block(previous.number, deadline)
            if retained is None or retained.header != previous:
                raise BranchConflict("Retained checkpoint is not on the observed canonical branch.")

        next_number = previous.number + 1 if previous else self.catalog.chain.start_block
        candidate = await self._block(next_number, deadline)
        if candidate is None:
            # Null may mean unavailable history, not just "at tip". A head
            # at or beyond the missing height cannot establish completion.
            response = await self.rpc.call(
                "eth_getBlockByNumber",
                ["latest", False],
                deadline=deadline,
            )
            if type(response) is not dict:
                raise RpcUnavailable("Selected provider did not identify its current head.")

            head_number = _quantity(response.get("number"))
            head = _block(response, head_number)
            if head.header.number >= next_number:
                raise RpcUnavailable("Selected provider omitted a block below its reported head.")
            if previous is not None and head.header.number < previous.number:
                raise BranchConflict("Provider head is below the retained checkpoint.")
            if previous is not None and head.header.number == previous.number and head.header != previous:
                raise BranchConflict("Provider head contradicts the retained canonical block.")
            return None

        if previous is not None and candidate.header.parent_hash != previous.hash:
            raise BranchConflict("Candidate block does not extend the retained checkpoint.")

        events = []
        raw_event_bytes = 0

        # Query EVERY configured (address, topic0), including empty results.
        # Reader-specific customer/channel predicates would silently omit
        # events while falsely advancing the complete history prefix.
        for contract in self.catalog.contracts:
            for descriptor in contract.descriptor.events:
                filter_ = {
                    "blockHash": "0x" + candidate.header.hash.hex(),
                    "address": "0x" + contract.address.hex(),
                    "topics": ["0x" + descriptor.topic0.hex()],
                }
                response = await self.rpc.call(
                    "eth_getLogs",
                    [filter_],
                    deadline=deadline,
                )
                if type(response) is not list:
                    raise RpcProtocolError("Selected RPC log query did not return an array.")

                # Each selected response must itself be ordered. Sorting or
                # silently deduplicating it would conceal conflicting evidence.
                previous_position = None
                for record in response:
                    event = _event(
                        record,
                        observed=candidate,
                        contract_address=contract.address,
                        descriptor=descriptor,
                        limits=self.catalog.limits,
                    )
                    position = (event.transaction_index, event.log_index)
                    if previous_position is not None and position <= previous_position:
                        raise RpcProtocolError("Selected RPC log response is duplicated or out of order.")
                    previous_position = position

                    events.append(event)
                    if len(events) > self.catalog.limits.max_logs_per_block:
                        raise ResourceExceeded("Selected block exceeds its configured log-count budget.")

                    # Individual bounded RPC responses can collectively
                    # exceed the per-block event budget.
                    raw_event_bytes += 52 + 32 * len(event.topics) + len(event.data)
                    if raw_event_bytes > MAX_BLOCK_RAW_BYTES:
                        raise ResourceExceeded("Selected block exceeds its aggregate raw-event budget.")

        # Local ordering is not enough across selectors. Global log indices
        # may have gaps for unselected events, but must never collide or
        # contradict the block's transaction order.
        events.sort(key=lambda event: event.log_index)
        previous_position = None
        for event in events:
            position = (event.transaction_index, event.log_index)
            if previous_position is not None and (
                event.log_index <= previous_position[1]
                or event.transaction_index < previous_position[0]
            ):
                raise RpcProtocolError("Selected RPC events contradict block-global occurrence order.")
            previous_position = position

        # Hash-pinned queries identify one block, which can nevertheless
        # become noncanonical during acquisition. Reobserve its identity
        # by height before SQL; this is consistency checking, not finality.
        latest = await self._block(next_number, deadline)
        if latest != candidate:
            raise BranchConflict("Candidate block changed during selected-log acquisition.")

        return await self.journal.publish_block(
            block=candidate.header,
            events=tuple(events),
        )

    async def repair_to(self, *, ancestor: Block) -> Progress:
        """Require an explicitly chosen canonical ancestor for suffix repair.

        Reorganizations never trigger automatic deletion. RPC establishes
        the current block hash; the journal independently checks the retained
        ancestor and changes generation atomically. No RPC occurs in SQL.
        """
        deadline = time.monotonic() + self.catalog.limits.operation_timeout_seconds
        observed = await self._block(ancestor.number, deadline)
        if observed is None or observed.header.hash != ancestor.hash:
            raise BranchConflict("Selected repair ancestor is not canonical at the provider.")
        return await self.journal.repair_to(ancestor=observed.header)


@asynccontextmanager
async def open_writer(*, catalog: Catalog, database_dsn: str, rpc_url: str):
    """Own one selected HTTP session and one exclusive journal writer.

    Verify chain/genesis before yielding. Both resources close on exit;
    an uncertain COMMIT retires the journal instead of being retried here.
    """
    selected_url = _rpc_url(rpc_url)

    async with open_journal(catalog=catalog, database_dsn=database_dsn) as journal:
        async with httpx.AsyncClient(trust_env=False, timeout=None) as http:
            writer = Writer(
                catalog=catalog,
                journal=journal,
                rpc=RPC(client=http, url=selected_url, limits=catalog.limits),
            )
            await writer.verify_source()
            yield writer
