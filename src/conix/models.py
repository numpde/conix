# Adapted from numpde/pay-to-play@2a1adb00b2eba9a18b49c92ca39a69ecf59bb110
# src/local_observer/models.py; shared protocol/ABI semantics.
"""Immutable chain facts and a replaceable observation API, without RPC or ABI.

Checkpoints and cursors identify one backend generation. They are read handles,
not durable accounting records, proof of finality, or authorization to spend.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Protocol
from uuid import UUID

from eth_typing import ChecksumAddress


class ObservationUnavailable(Exception):
    """The backend could not establish the requested chain facts; retry may help."""


class ObservationProtocolError(Exception):
    """The configured source or its response cannot represent the supported protocol."""


class HistoryUnavailable(Exception):
    """A history handle is invalid or retired; explicitly resynchronize before using a new one."""


class Phase(IntEnum):
    OPERATING = 0
    SETTLING = 1
    REFUND_ONLY = 2


@dataclass(frozen=True, slots=True)
class Deployment:
    """Chain and deployed contract identity, independent of RPC transport."""

    chain_id: int
    address: ChecksumAddress


@dataclass(frozen=True, slots=True)
class Block:
    """Block identity and parent link, not a claim about financial finality."""

    number: int
    hash: bytes
    parent_hash: bytes
    timestamp: int


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A processed window's boundary; collection_count is a generation-local offset."""

    deployment: Deployment
    generation: UUID
    block: Block
    collection_count: int
    upstream_head: Block


@dataclass(frozen=True, slots=True)
class Position:
    """Authoritative contract fields; no locally derived spending balance."""

    customer: ChecksumAddress
    channel_id: int
    payee: ChecksumAddress
    claim_period: int
    channel_count: int
    admin: ChecksumAddress
    collector: ChecksumAddress
    balance: int
    voucher_paid: int
    requested_amount: int
    ready_at: int
    effective_balance: int
    voucher_key: ChecksumAddress
    scheduled_refund_at: int
    refund_at: int
    settle_at: int
    close_at: int
    escrow_total: int
    refund_recipient: ChecksumAddress
    phase: Phase


@dataclass(frozen=True, slots=True)
class PositionObservation:
    """Contract state pinned to a checkpoint with its own observation time."""

    checkpoint: Checkpoint
    observed_at: datetime
    asset: ChecksumAddress
    position: Position


@dataclass(frozen=True, slots=True)
class CanonicalEvidence:
    """Requested canonical block facts and an optional source-reported finalized block."""

    checkpoint: Checkpoint
    blocks: tuple[Block, ...]
    finalized: Block | None


@dataclass(frozen=True, slots=True)
class Collection:
    """A transfer with time from its identified canonical block, not poll time."""

    block_number: int
    block_hash: bytes
    block_timestamp: int
    transaction_hash: bytes
    transaction_index: int
    log_index: int
    customer: ChecksumAddress
    channel_id: int
    payee: ChecksumAddress
    amount: int
    voucher_paid: int
    invoice_hash: bytes


@dataclass(frozen=True, slots=True)
class CollectionCursor:
    """Resume after a sequence, including at a newer checkpoint in this generation."""

    generation: UUID
    next_sequence: int


@dataclass(frozen=True, slots=True)
class CollectionPage:
    """Events within history_start..checkpoint; complete never claims earlier coverage.

    history_start is the first retained block. complete means the requested
    cursor has reached this checkpoint, not the upstream tip or deployment start.
    """

    checkpoint: Checkpoint
    history_start: Block
    voucher_collected_events: tuple[Collection, ...]
    cursor: CollectionCursor
    complete: bool


class Observer(Protocol):
    """Read latest state, then page its collection prefix without selecting old state."""

    async def latest_checkpoint(self) -> Checkpoint:
        """Return the latest completely scanned collection checkpoint and upstream head."""
        ...

    async def latest_position(self, customer: ChecksumAddress, channel_id: int) -> PositionObservation:
        """Return authoritative position values at one backend-selected checkpoint."""
        ...

    async def canonical_evidence(
        self, checkpoint: Checkpoint, block_numbers: tuple[int, ...], *, include_finalized: bool
    ) -> CanonicalEvidence:
        """Read requested branch facts at an identified checkpoint for risk decisions."""
        ...

    async def voucher_collected_events(
        self, checkpoint: Checkpoint, *, cursor: CollectionCursor | None, limit: int
    ) -> CollectionPage:
        """Page only the collection prefix of a returned checkpoint; invalid handles fail."""
        ...
