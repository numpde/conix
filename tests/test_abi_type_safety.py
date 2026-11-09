"""Executable ABI boundary and wire-log regressions for the CURRENT Conix decoder.

These tests are not evidence that the proposed generic event store or reader
exists. They provide fixed, independent scalar word oracles and exercise the
actual deployed PayToPlay decoder, without substituting a model of Conix.
"""
import random
import unittest

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from conix.models import Block, Deployment
from conix.pay_to_play import COLLECTION_SIGNATURE, PayToPlay, _abi
from conix.rpc import RpcProtocolError


CONTRACT = to_checksum_address(bytes.fromhex("a1" * 20))
CUSTOMER = to_checksum_address(bytes.fromhex("00" * 19 + "01"))
PAYEE = to_checksum_address(bytes.fromhex("ff" * 19 + "01"))
BLOCK = Block(123, bytes.fromhex("b7" * 32), bytes.fromhex("c8" * 32), 1710000123)


def word(integer):
    """Independent EVM 256-bit word oracle; do not invoke the ABI encoder."""
    return (integer % (1 << 256)).to_bytes(32, "big")


def decode_one(abi_type, encoded):
    return _abi((abi_type,), encoded, "type-safety fixture")[0]


def collection_log(*, channel, amount, cumulative_paid, invoice_hash):
    topics = encode(
        ("address", "uint256", "address"),
        (CUSTOMER, channel, PAYEE),
    )

    return {
        "address": CONTRACT,
        "topics": [
            "0x" + keccak(text=COLLECTION_SIGNATURE).hex(),
            *("0x" + topics[index:index + 32].hex() for index in range(0, len(topics), 32)),
        ],
        "data": "0x" + encode(
            ("uint256", "uint256", "bytes32"),
            (amount, cumulative_paid, invoice_hash),
        ).hex(),
        "blockNumber": hex(BLOCK.number),
        "blockHash": "0x" + BLOCK.hash.hex(),
        "transactionHash": "0x" + "ab" * 32,
        "transactionIndex": "0x2",
        "logIndex": "0x9",
        "removed": False,
    }


class AbiScalarBoundaryContracts(unittest.TestCase):
    """Literal-word oracles expose signedness and width errors independently."""

    def test_all_unsigned_abi_widths_and_both_signed_boundaries(self):
        for bits in range(8, 257, 8):
            unsigned_samples = {
                0, 1, (1 << (bits - 1)) - 1, 1 << (bits - 1), (1 << bits) - 1,
            }
            signed_samples = {
                -(1 << (bits - 1)), -(1 << (bits - 1)) + 1, -1,
                0, 1, (1 << (bits - 1)) - 2, (1 << (bits - 1)) - 1,
            }
            for value in sorted(unsigned_samples):
                with self.subTest(abi_type=f"uint{bits}", value=value):
                    self.assertEqual(decode_one(f"uint{bits}", word(value)), value)

            for value in sorted(signed_samples):
                with self.subTest(abi_type=f"int{bits}", value=value):
                    self.assertEqual(decode_one(f"int{bits}", word(value)), value)

    def test_noncanonical_unsigned_width_or_signed_extension_is_refused(self):
        for bits in range(8, 256, 8):
            with self.subTest(abi_type=f"uint{bits}"), self.assertRaises(RpcProtocolError):
                decode_one(f"uint{bits}", word(1 << bits))

            with self.subTest(abi_type=f"int{bits}"), self.assertRaises(RpcProtocolError):
                # Bit pattern has a negative sign in int<bits> but no sign
                # extension into the upper 256-bit word.
                decode_one(f"int{bits}", word(1 << (bits - 1)))

    def test_larger_than_signed_postgres_and_json_precision_boundaries(self):
        values = (
            32767, 32768, 65535,
            2147483647, 2147483648, 4294967295,
            (1 << 63) - 1, 1 << 63, (1 << 64) - 1,
            (1 << 127) - 1, 1 << 127, (1 << 128) - 1,
            1 << 255, (1 << 256) - 1,
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(decode_one("uint256", word(value)), value)
                self.assertIs(type(decode_one("uint256", word(value))), int)

    def test_all_fixed_bytes_widths_preserve_contents_not_abi_padding(self):
        for size in range(1, 33):
            samples = (
                bytes(size),
                b"\xff" * size,
                b"\x00" * (size - 1) + b"\x01",
                b"\x01" + b"\x00" * (size - 1),
            )
            for value in samples:
                encoded = value + bytes(32 - size)
                with self.subTest(size=size, value=value.hex()):
                    self.assertEqual(decode_one(f"bytes{size}", encoded), value)
                    self.assertEqual(len(decode_one(f"bytes{size}", encoded)), size)

                if size < 32:
                    with self.subTest(size=size, kind="noncanonical padding"):
                        with self.assertRaises(RpcProtocolError):
                            decode_one(f"bytes{size}", value + bytes(31 - size) + b"\x01")

    def test_bool_and_address_word_identity(self):
        self.assertIs(decode_one("bool", word(0)), False)
        self.assertIs(decode_one("bool", word(1)), True)

        for malformed in (word(2), word(255), word((1 << 256) - 1)):
            with self.subTest(malformed=malformed.hex()), self.assertRaises(RpcProtocolError):
                decode_one("bool", malformed)

        address_bytes = bytes.fromhex("00" * 19 + "ff")
        self.assertEqual(
            to_checksum_address(decode_one("address", bytes(12) + address_bytes)),
            to_checksum_address(address_bytes),
        )
        with self.assertRaises(RpcProtocolError):
            decode_one("address", b"\x01" + bytes(11) + address_bytes)

    def test_rejects_truncation_and_unexpected_suffix(self):
        for abi_type, valid in (
            ("uint256", word(17)),
            ("int16", word(-7)),
            ("bool", word(1)),
            ("bytes31", bytes(31) + b"\x00"),
        ):
            for corrupted in (valid[:-1], valid + b"\x00", valid[:17]):
                with self.subTest(abi_type=abi_type, size=len(corrupted)):
                    with self.assertRaises(RpcProtocolError):
                        decode_one(abi_type, corrupted)


class AbiCompoundBoundaryContracts(unittest.TestCase):
    """Round-trip shapes via a separately supplied eth-abi encoder."""

    def test_dynamic_bytes_length_boundaries_and_utf8(self):
        for size in (0, 1, 31, 32, 33, 255, 256):
            value = bytes((index * 37 + 11) % 256 for index in range(size))
            with self.subTest(size=size):
                self.assertEqual(decode_one("bytes", encode(("bytes",), (value,))), value)

        for value in ("", "ASCII", "café", "e\u0301", "東西", "a\x00b"):
            with self.subTest(value=value):
                self.assertEqual(decode_one("string", encode(("string",), (value,))), value)

    def test_fixed_dynamic_and_nested_array_shapes(self):
        examples = (
            ("uint16[2]", (0, 65535)),
            ("uint16[]", ()),
            ("uint256[]", (0, 1 << 255, (1 << 256) - 1)),
            ("int40[3]", (-(1 << 39), -1, (1 << 39) - 1)),
            ("bytes2[]", (b"\x00\xff", b"\xff\x00")),
            ("uint16[2][]", ((0, 65535), (123, 456))),
            ("uint16[][2]", ((1, 2, 3), (4,))),
            ("uint16[][]", ((1, 2), (), (3,))),
            ("(uint256,bytes2)[]", ((1 << 255, b"\x00\xff"), (9, b"\xff\x00"))),
            ("((uint32,bytes2)[],string[])", (((4, b"\x00\x01"),), ("a", "b"))),
        )
        for abi_type, value in examples:
            with self.subTest(abi_type=abi_type):
                self.assertEqual(
                    decode_one(abi_type, encode((abi_type,), (value,))),
                    value,
                )

    def test_reproducible_random_nested_values(self):
        for seed in range(50):
            generator = random.Random(seed)
            values = tuple(
                (
                    generator.randrange(1 << 16),
                    generator.randrange(1 << 256),
                    generator.randbytes(8),
                )
                for _ in range(generator.randrange(0, 6))
            )
            abi_type = "(uint16,uint256,bytes8)[]"
            with self.subTest(seed=seed):
                encoded = encode((abi_type,), (values,))
                self.assertEqual(decode_one(abi_type, encoded), values)

    def test_bad_dynamic_offsets_and_trailing_bytes_fail_closed(self):
        original = encode(("bytes",), (b"payload",))
        mutants = (
            word(1) + original[32:],
            word(2**128) + original[32:],
            word(0) + original[32:],
            original[:-1],
            original + word(0),
        )
        for index, corrupted in enumerate(mutants):
            with self.subTest(mutant=index), self.assertRaises(RpcProtocolError):
                decode_one("bytes", corrupted)


class CollectionWireContracts(unittest.TestCase):
    """Exercise the real PayToPlay log decoder on adversarial boundary logs."""

    def setUp(self):
        self.contract = PayToPlay(object(), Deployment(31337, CONTRACT))

    def test_full_uint256_and_exact_occurrence_identity(self):
        full = (1 << 256) - 1
        log = collection_log(
            channel=full,
            amount=1 << 255,
            cumulative_paid=full,
            invoice_hash=b"\x00" * 31 + b"\xff",
        )
        observed = self.contract.collection(log, BLOCK)

        self.assertEqual(observed.channel_id, full)
        self.assertEqual(observed.amount, 1 << 255)
        self.assertEqual(observed.voucher_paid, full)
        self.assertEqual(observed.invoice_hash, b"\x00" * 31 + b"\xff")
        self.assertEqual((observed.customer, observed.payee), (CUSTOMER, PAYEE))
        self.assertEqual((observed.block_number, observed.block_hash), (BLOCK.number, BLOCK.hash))
        self.assertEqual((observed.transaction_index, observed.log_index), (2, 9))
        self.assertEqual(observed.transaction_hash, bytes.fromhex("ab" * 32))

    def test_raw_event_topics_and_data_are_not_reinterpreted(self):
        values = (
            (1, 1, 1, bytes(32)),
            (1 << 64, 1 << 128, 1 << 255, b"\xff" * 32),
            ((1 << 256) - 1, (1 << 256) - 1, (1 << 256) - 1, b"\xff" + bytes(31)),
        )
        for channel, amount, paid, invoice in values:
            with self.subTest(channel=channel, amount=amount):
                observed = self.contract.collection(
                    collection_log(
                        channel=channel,
                        amount=amount,
                        cumulative_paid=paid,
                        invoice_hash=invoice,
                    ),
                    BLOCK,
                )
                self.assertEqual(
                    (observed.channel_id, observed.amount, observed.voucher_paid, observed.invoice_hash),
                    (channel, amount, paid, invoice),
                )

    def test_malformed_and_wrong_source_logs_are_rejected(self):
        valid = collection_log(
            channel=12,
            amount=7,
            cumulative_paid=11,
            invoice_hash=b"\x01" * 32,
        )
        malformed = (
            {"topics": valid["topics"][:3]},
            {"topics": ["0x" + "ff" * 32, *valid["topics"][1:]]},
            {"data": valid["data"][:-2]},
            {"data": valid["data"] + "00"},
            {"address": CUSTOMER},
            {"removed": True},
            {"blockNumber": hex(BLOCK.number + 1)},
            {"blockHash": "0x" + "aa" * 32},
            {"transactionHash": "0x12"},
            {"logIndex": -1},
        )
        for index, changed in enumerate(malformed):
            with self.subTest(index=index), self.assertRaises(RpcProtocolError):
                self.contract.collection({**valid, **changed}, BLOCK)


if __name__ == "__main__":
    unittest.main()
