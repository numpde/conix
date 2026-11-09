"""Immutable EVM event wire oracles for the future generic Conix decoder.

These vectors are constructed from literal 32-byte ABI words, not by calling
the decoder or eth_abi.encode. Expected decoded values are independent data.

They are TEST DATA, not evidence that generic event indexing exists yet.
"""
from dataclasses import dataclass

from eth_utils import keccak, to_checksum_address


def word(number):
    return number.to_bytes(32, "big")


def signed_word(number):
    return (number % (1 << 256)).to_bytes(32, "big")


def address_word(address):
    return bytes(12) + bytes.fromhex(address[2:])


def signature_topic(signature):
    return keccak(text=signature)


def padded(data):
    return data + bytes((-len(data)) % 32)


FROM = to_checksum_address(bytes.fromhex("00" * 19 + "01"))
TO = to_checksum_address(bytes.fromhex("ff" * 19 + "00"))
TRANSFER_SIGNATURE = "Transfer(address,address,uint256)"

ERC20_ABI = {
    "type": "event",
    "name": "Transfer",
    "anonymous": False,
    "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "value", "type": "uint256", "indexed": False},
    ],
}
ERC721_ABI = {
    "type": "event",
    "name": "Transfer",
    "anonymous": False,
    "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "tokenId", "type": "uint256", "indexed": True},
    ],
}
MIXED_ABI = {
    "type": "event",
    "name": "Mixed",
    "anonymous": False,
    "inputs": [
        {"name": "small", "type": "uint8", "indexed": True},
        {"name": "signed", "type": "int16", "indexed": True},
        {"name": "fixed", "type": "bytes4", "indexed": False},
        {"name": "text", "type": "string", "indexed": False},
    ],
}
HASHED_ABI = {
    "type": "event",
    "name": "Metadata",
    "anonymous": False,
    "inputs": [
        {"name": "indexedText", "type": "string", "indexed": True},
        {"name": "value", "type": "bytes", "indexed": False},
    ],
}


@dataclass(frozen=True, slots=True)
class EventVector:
    name: str
    contract: str
    abi: dict
    topics: tuple[bytes, ...]
    data: bytes
    expected: tuple
    hashed_arguments: tuple[int, ...] = ()


ERC20 = EventVector(
    name="erc20-max-uint256",
    contract="erc20",
    abi=ERC20_ABI,
    topics=(signature_topic(TRANSFER_SIGNATURE), address_word(FROM), address_word(TO)),
    data=word((1 << 256) - 1),
    expected=(FROM, TO, (1 << 256) - 1),
)

ERC721 = EventVector(
    name="erc721-same-signature-indexed-uint256",
    contract="erc721",
    abi=ERC721_ABI,
    topics=(
        signature_topic(TRANSFER_SIGNATURE),
        address_word(FROM),
        address_word(TO),
        word(1 << 255),
    ),
    data=b"",
    expected=(FROM, TO, 1 << 255),
)

MIXED = EventVector(
    name="signed-and-fixed-bytes-with-dynamic-string",
    contract="mixed",
    abi=MIXED_ABI,
    topics=(
        signature_topic("Mixed(uint8,int16,bytes4,string)"),
        word(255),
        signed_word(-32768),
    ),
    data=(
        b"\x00\xff\x00\x01" + bytes(28)
        + word(64)
        + word(3)
        + padded(b"xyz")
    ),
    expected=(255, -32768, b"\x00\xff\x00\x01", "xyz"),
)

INDEXED_TEXT = "café"
HASHED = EventVector(
    name="indexed-dynamic-string-is-a-hash-not-text",
    contract="metadata",
    abi=HASHED_ABI,
    topics=(
        signature_topic("Metadata(string,bytes)"),
        keccak(INDEXED_TEXT.encode("utf-8")),
    ),
    data=(
        word(32)  # A single dynamic, nonindexed bytes argument.
        + word(2)
        + padded(b"\x00\xff")
    ),
    expected=(keccak(INDEXED_TEXT.encode("utf-8")), b"\x00\xff"),
    hashed_arguments=(0,),
)

EVENT_VECTORS = (ERC20, ERC721, MIXED, HASHED)
