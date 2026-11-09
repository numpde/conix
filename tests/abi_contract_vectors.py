"""Independent literal-wire fixtures for the proposed generic Conix ABI contract.

No Conix codec or eth_abi encoder constructs these expected bytes.
The PayToPlay event and position layout follow
numpde/dapp32/dapps/pay-to-play/src/PayToPlay.sol (2026-11-09).
These are facts/test oracles; they do not implement generic indexing.
"""
from dataclasses import dataclass

from abi_golden_vectors import (
    EventVector, FROM, TO, address_word, padded, signature_topic, word,
)


VOUCHER_SIGNATURE = "VoucherCollected(address,uint256,address,uint256,uint256,bytes32)"
PAY_TO_PLAY_ABI = {
    "type": "event",
    "name": "VoucherCollected",
    "anonymous": False,
    "inputs": [
        {"name": "customer", "type": "address", "indexed": True},
        {"name": "channelId", "type": "uint256", "indexed": True},
        {"name": "payee", "type": "address", "indexed": True},
        {"name": "amount", "type": "uint256", "indexed": False},
        {"name": "voucherPaid", "type": "uint256", "indexed": False},
        {"name": "invoiceHash", "type": "bytes32", "indexed": False},
    ],
}
INVOICE_HASH = b"\x00" * 31 + b"\xff"
PAY_TO_PLAY = EventVector(
    name="actual-pay-to-play-voucher-collected",
    contract="payment",
    abi=PAY_TO_PLAY_ABI,
    topics=(
        signature_topic(VOUCHER_SIGNATURE), address_word(FROM),
        word((1 << 256) - 1), address_word(TO),
    ),
    data=word(1 << 255) + word((1 << 256) - 1) + INVOICE_HASH,
    expected=(FROM, (1 << 256) - 1, TO, 1 << 255, (1 << 256) - 1, INVOICE_HASH),
)


COMPOUND_SIGNATURE = "Complex(address,(uint16,bytes8)[],string[],bytes)"
COMPOUND_ABI = {
    "type": "event",
    "name": "Complex",
    "anonymous": False,
    "inputs": [
        {"name": "source", "type": "address", "indexed": True},
        {"name": "entries", "type": "tuple[]", "indexed": False, "components": [
            {"name": "code", "type": "uint16"},
            {"name": "marker", "type": "bytes8"},
        ]},
        {"name": "labels", "type": "string[]", "indexed": False},
        {"name": "payload", "type": "bytes", "indexed": False},
    ],
}
MARKER_A = b"\x00\xff\x00\x01\x00\x00\xfe\x00"
MARKER_B = b"abcdefgh"
PAYLOAD = bytes(32) + b"\xff"

# Three dynamic nonindexed arguments: head is 3 * 32 bytes.
# tuple[] tail: length + two static (uint16,bytes8) tuples = 160 bytes.
COMPOUND_TUPLES = (
    word(2) + word(65535) + padded(MARKER_A) + word(1) + padded(MARKER_B)
)
# Array-element offsets are relative to the position after the length word.
COMPOUND_STRINGS = (
    word(2) + word(64) + word(96)
    + word(0)
    + word(3) + padded("€".encode("utf-8"))
)
COMPOUND_PAYLOAD = word(33) + padded(PAYLOAD)
COMPOUND = EventVector(
    name="nested-tuple-array-unicode-and-33-byte-payload",
    contract="compound",
    abi=COMPOUND_ABI,
    topics=(signature_topic(COMPOUND_SIGNATURE), address_word(FROM)),
    data=(
        word(96) + word(256) + word(448)
        + COMPOUND_TUPLES + COMPOUND_STRINGS + COMPOUND_PAYLOAD
    ),
    expected=(
        FROM,
        ((65535, MARKER_A), (1, MARKER_B)),
        ("", "€"),
        PAYLOAD,
    ),
)


POSITION_FIELDS = (
    ("customer", "address"), ("channelId", "uint256"), ("payee", "address"),
    ("claimPeriod", "uint64"), ("channelCount", "uint256"), ("admin", "address"),
    ("collector", "address"), ("balance", "uint256"), ("voucherPaid", "uint256"),
    ("requestedAmount", "uint256"), ("readyAt", "uint256"),
    ("effectiveBalance", "uint256"), ("voucherKey", "address"),
    ("scheduledRefundAt", "uint256"), ("refundAt", "uint256"),
    ("settleAt", "uint256"), ("closeAt", "uint256"), ("escrowTotal", "uint256"),
    ("refundRecipient", "address"), ("phase", "uint8"), ("actions", "string[]"),
)
POSITION_ABI_TYPE = "(" + ",".join(abi_type for _, abi_type in POSITION_FIELDS) + ")"
POSITION_SAMPLE = (
    FROM, 1 << 255, TO, 86400, 2, FROM, TO, 1 << 200, 1 << 199, 1,
    1730000000, (1 << 200) - 1, FROM, 1730000001, 1730000100,
    1730000200, 1730000300, 1 << 210, TO, 1, ("collect", "refund"),
)
POSITION_WORDS = (
    address_word(FROM), word(1 << 255), address_word(TO), word(86400),
    word(2), address_word(FROM), address_word(TO), word(1 << 200),
    word(1 << 199), word(1), word(1730000000), word((1 << 200) - 1),
    address_word(FROM), word(1730000001), word(1730000100),
    word(1730000200), word(1730000300), word(1 << 210),
    address_word(TO), word(1),
)
POSITION_ACTIONS = (
    word(2) + word(64) + word(128)
    + word(7) + padded(b"collect")
    + word(6) + padded(b"refund")
)
# Single dynamic tuple return: outer offset, 20 static words, one inner
# offset to string[] (21 * 32), and the complete string[] tail.
POSITION_RETURN = word(32) + b"".join(POSITION_WORDS) + word(21 * 32) + POSITION_ACTIONS
POSITION_SIGNATURE = "viewPosition(address,uint256,address)"
POSITION_CALL = (
    signature_topic(POSITION_SIGNATURE)[:4]
    + address_word(FROM) + word((1 << 256) - 1) + address_word(TO)
)


EXTRA_EVENT_VECTORS = (PAY_TO_PLAY, COMPOUND)
