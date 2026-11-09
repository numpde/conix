"""Check the independent wire fixtures that will drive generic Conix tests.

This checks the validity of the test oracles, NOT Conix's future generic
catalog, indexing completeness, indexed hashing or event query behavior.
"""
import unittest

from eth_abi import decode, encode
from eth_utils import keccak

from abi_golden_vectors import ERC20, ERC721, HASHED, MIXED, EVENT_VECTORS


class EventWireOracleContracts(unittest.TestCase):

    def test_erc20_and_erc721_share_topic0_but_not_indexed_layout(self):
        self.assertEqual(ERC20.topics[0], ERC721.topics[0])
        self.assertEqual(len(ERC20.topics), 3)
        self.assertEqual(len(ERC721.topics), 4)
        self.assertEqual(len(ERC20.data), 32)
        self.assertEqual(ERC721.data, b"")
        self.assertEqual(
            tuple(item["indexed"] for item in ERC20.abi["inputs"]),
            (True, True, False),
        )
        self.assertEqual(
            tuple(item["indexed"] for item in ERC721.abi["inputs"]),
            (True, True, True),
        )
        self.assertEqual(
            decode(("uint256",), ERC20.data),
            ((1 << 256) - 1,),
        )
        self.assertEqual(
            decode(("uint256",), ERC721.topics[3]),
            (1 << 255,),
        )

    def test_literal_topics_represent_contract_addresses(self):
        self.assertEqual(
            decode(("address",), ERC20.topics[1])[0].lower(),
            ERC20.expected[0].lower(),
        )
        self.assertEqual(
            decode(("address",), ERC20.topics[2])[0].lower(),
            ERC20.expected[1].lower(),
        )
        self.assertEqual(
            (ERC20.topics[1], ERC20.topics[2]),
            (ERC721.topics[1], ERC721.topics[2]),
        )

    def test_signed_topic_and_dynamic_unindexed_payloads(self):
        self.assertEqual(decode(("uint8",), MIXED.topics[1]), (255,))
        self.assertEqual(decode(("int16",), MIXED.topics[2]), (-32768,))
        self.assertEqual(
            decode(("bytes4", "string"), MIXED.data),
            (b"\x00\xff\x00\x01", "xyz"),
        )
        self.assertEqual(
            MIXED.data[:32],
            b"\x00\xff\x00\x01" + bytes(28),
        )

    def test_indexed_string_is_hashed_and_original_is_not_recoverable(self):
        self.assertEqual(HASHED.hashed_arguments, (0,))
        self.assertEqual(HASHED.topics[1], keccak("café".encode("utf-8")))
        self.assertEqual(HASHED.expected[0], HASHED.topics[1])
        self.assertEqual(decode(("bytes",), HASHED.data), (b"\x00\xff",))
        self.assertEqual(HASHED.data[:32], (32).to_bytes(32, "big"))
        self.assertNotEqual(HASHED.topics[1], "café".encode("utf-8").ljust(32, b"\x00"))

    def test_all_golden_records_are_bounded_unambiguous_and_consistent(self):
        self.assertEqual(len({entry.name for entry in EVENT_VECTORS}), len(EVENT_VECTORS))
        for entry in EVENT_VECTORS:
            with self.subTest(name=entry.name):
                self.assertEqual(len(entry.topics[0]), 32)
                self.assertTrue(all(len(topic) == 32 for topic in entry.topics))
                self.assertEqual(len(entry.data) % 32, 0)
                self.assertEqual(len(entry.expected), len(entry.abi["inputs"]))
                self.assertEqual(
                    len(entry.topics),
                    1 + sum(bool(item["indexed"]) for item in entry.abi["inputs"]),
                )
                self.assertTrue(
                    all(0 <= argument < len(entry.abi["inputs"]) for argument in entry.hashed_arguments)
                )


if __name__ == "__main__":
    unittest.main()
