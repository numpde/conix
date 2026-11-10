"""One concrete, block-hash-pinned EVM log scanner and publisher.

The scanner obtains complete selected-log evidence for exactly one block,
checks canonicality before publication, and delegates the SQL mutation to
the existing journal. It does not support receipt or range acquisition.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import re
import time

import httpx

from conix.abi import MalformedABIData, ResourceExceeded, decode_event
from conix.catalog import Catalog, _rpc_url
from conix.journal import Block, Journal, Progress, RawEvent, open_journal
from conix.rpc import RPC, RpcProtocolError, RpcUnavailable


QUANTITY = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]*)\Z")
HEX = re.compile(r"0x(?:[0-9a-fA-F]{2})*\Z")
MAX_BLOCK_RAW_BYTES = 8 * 1024 * 1024


class BranchConflict(RuntimeError):
    """RPC observations contradict the retained or candidate canonical branch."""


@dataclass(frozen=True, slots=True)
class ObservedBlock:
    """One numbered block and its original transaction-hash inventory."""

    header: Block
    transactions: tuple[bytes, ...]


def _quantity(value: object, label: str) -> int:
    if type(value) is not str or QUANTITY.fullmatch(value) is None:
        raise RpcProtocolError(f"RPC {label} is not a canonical hex quantity.")

    number = int(value, 16)
    if number >= 2**63:
        raise ResourceExceeded(f"RPC {label} exceeds the supported integer range.")
    return number


def _bytes(value: object, size: int | None, label: str) -> bytes:
    if type(value) is not str or HEX.fullmatch(value) is None:
        raise RpcProtocolError(f"RPC {label} is not a hexadecimal byte string.")

    decoded = bytes.fromhex(value[2:])
    if size is not None and len(decoded) != size:
        raise RpcProtocolError(f"RPC {label} has an incorrect byte width.")
    return decoded


def _block(response: object, expected_number: int) -> ObservedBlock:
    if type(response) is not dict:
        raise RpcProtocolError("RPC block header is not an object.")

    number = _quantity(response.get("number"), "block number")
    if number != expected_number:
        raise RpcProtocolError(f"{number = }, {expected_number = }")

    block = Block(
        number=number,
        hash=_bytes(response.get("hash"), 32, "block hash"),
        parent_hash=_bytes(response.get("parentHash"), 32, "parent hash"),
        timestamp=_quantity(response.get("timestamp"), "block timestamp"),
    )
    transaction_hashes = response.get("transactions")
    if type(transaction_hashes) is not list:
        raise RpcProtocolError("RPC block lacks its transaction-hash inventory.")

    return ObservedBlock(
        header=block,
        transactions=tuple(
            _bytes(transaction_hash, 32, "block transaction hash")
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
    if type(response) is not dict or response.get("removed", False) is not False:
        raise RpcProtocolError("Selected RPC log is malformed or marked removed.")

    header = observed.header
    if (
        _bytes(response.get("blockHash"), 32, "log block hash") != header.hash
        or _quantity(response.get("blockNumber"), "log block number") != header.number
        or _bytes(response.get("address"), 20, "log emitting address") != contract_address
    ):
        raise RpcProtocolError("Selected RPC log belongs to a different block or contract.")

    transaction_index = _quantity(response.get("transactionIndex"), "transaction index")
    log_index = _quantity(response.get("logIndex"), "log index")
    transaction_hash = _bytes(response.get("transactionHash"), 32, "transaction hash")

    if (
        transaction_index >= len(observed.transactions)
        or observed.transactions[transaction_index] != transaction_hash
    ):
        raise RpcProtocolError("Selected log contradicts the block transaction inventory.")

    declared_topics = response.get("topics")
    if type(declared_topics) is not list:
        raise RpcProtocolError("Selected RPC log does not contain a topic array.")

    topics = tuple(_bytes(topic, 32, "log topic") for topic in declared_topics)
    data = _bytes(response.get("data"), None, "log data")

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


class Writer:
    """Check one selected block's entire evidence before committing any row."""

    def __init__(self, *, catalog: Catalog, journal: Journal, rpc: RPC):
        self.catalog = catalog
        self.journal = journal
        self.rpc = rpc

    async def _block(self, number: int, deadline: float) -> ObservedBlock | None:
        response = await self.rpc.call(
            "eth_getBlockByNumber",
            [hex(number), False],
            deadline=deadline,
        )
        return None if response is None else _block(response, number)

    async def verify_source(self) -> None:
        """Match the configured chain ID and genesis, before any scanning."""
        deadline = time.monotonic() + self.catalog.limits.operation_timeout_seconds
        chain_id = _quantity(
            (await self.rpc.call("eth_chainId", [], deadline=deadline)),
            "chain ID",
        )
        if chain_id != self.catalog.chain.chain_id:
            raise BranchConflict("Selected provider has a different EVM chain ID.")

        genesis = await self._block(0, deadline)
        if genesis is None or genesis.header.hash != self.catalog.chain.genesis_hash:
            raise BranchConflict("Selected provider has a different genesis block.")

    async def advance_one(self) -> Progress | None:
        """Publish the next complete selected block or return None at the tip.

        Even a block with zero selected logs must have a response for every
        configured event. A provider timeout, reorg or malformed result
        prevents the entire block from publishing.
        """
        deadline = time.monotonic() + self.catalog.limits.operation_timeout_seconds
        previous = self.journal.basis.last_block

        if previous is not None:
            retained = await self._block(previous.number, deadline)
            if retained is None or retained.header != previous:
                raise BranchConflict("Retained checkpoint is not on the observed canonical branch.")

        next_number = previous.number + 1 if previous else self.catalog.chain.start_block
        candidate = await self._block(next_number, deadline)
        if candidate is None:
            return None

        if previous is not None and candidate.header.parent_hash != previous.hash:
            raise BranchConflict("Candidate block does not extend the retained checkpoint.")

        events = []
        raw_event_bytes = 0

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

                    raw_event_bytes += 52 + 32 * len(event.topics) + len(event.data)
                    if raw_event_bytes > MAX_BLOCK_RAW_BYTES:
                        raise ResourceExceeded("Selected block exceeds its aggregate raw-event budget.")

        # Distinct selected contracts/events may have sparse global indices,
        # but they may never claim one occurrence twice or contradict tx order.
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

        latest = await self._block(next_number, deadline)
        if latest != candidate:
            raise BranchConflict("Candidate block changed during selected-log acquisition.")

        return await self.journal.publish_block(
            block=candidate.header,
            events=tuple(events),
        )

    async def repair_to(self, *, ancestor: Block) -> Progress:
        """Confirm an identified canonical ancestor before manual suffix repair."""
        deadline = time.monotonic() + self.catalog.limits.operation_timeout_seconds
        observed = await self._block(ancestor.number, deadline)
        if observed is None or observed.header.hash != ancestor.hash:
            raise BranchConflict("Selected repair ancestor is not canonical at the provider.")
        return await self.journal.repair_to(ancestor=observed.header)


@asynccontextmanager
async def open_writer(*, catalog: Catalog, database_dsn: str, rpc_url: str):
    """Own the checked PostgreSQL writer and its selected HTTP RPC session."""
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
