"""Qualify independent ABI fixtures, not the unimplemented generic core."""

import json
from pathlib import Path
import unittest

from eth_abi import decode, encode
from eth_utils import keccak

from abi_golden_vectors import EVENT_VECTORS
from abi_contract_vectors import (
    COMPOUND, COMPOUND_ABI, COMPOUND_SIGNATURE, EXTRA_EVENT_VECTORS,
    PAY_TO_PLAY, PAY_TO_PLAY_ABI, POSITION_ABI_TYPE, POSITION_CALL,
    POSITION_FIELDS, POSITION_RETURN, POSITION_SAMPLE, POSITION_SIGNATURE,
    VOUCHER_SIGNATURE,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "abi"


def definitions(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


class AbiFixtureContracts(unittest.TestCase):

    def test_actual_pay_to_play_event_matches_deployed_solidity(self):
        selected = next(item for item in definitions("pay_to_play") if item["type"] == "event")
        self.assertEqual(selected, PAY_TO_PLAY_ABI)
        self.assertEqual(PAY_TO_PLAY.topics[0], keccak(text=VOUCHER_SIGNATURE))
        self.assertEqual(len(PAY_TO_PLAY.topics), 4)
        self.assertEqual(PAY_TO_PLAY.data[-32:], PAY_TO_PLAY.expected[-1])
        self.assertEqual(
            decode(("address", "uint256", "address"), b"".join(PAY_TO_PLAY.topics[1:])),
            PAY_TO_PLAY.expected[:3],
        )
        self.assertEqual(
            decode(("uint256", "uint256", "bytes32"), PAY_TO_PLAY.data),
            PAY_TO_PLAY.expected[3:],
        )

    def test_compound_offsets_and_cross_encoder_equality(self):
        selected = next(item for item in definitions("compound") if item["type"] == "event")
        self.assertEqual(selected, COMPOUND_ABI)
        self.assertEqual(COMPOUND.topics[0], keccak(text=COMPOUND_SIGNATURE))
        self.assertEqual(
            tuple(int.from_bytes(COMPOUND.data[i:i + 32], "big") for i in (0, 32, 64)),
            (96, 256, 448),
        )
        types = ("(uint16,bytes8)[]", "string[]", "bytes")
        self.assertEqual(decode(types, COMPOUND.data), COMPOUND.expected[1:])
        self.assertEqual(encode(types, COMPOUND.expected[1:]), COMPOUND.data)

    def test_position_uses_real_21_field_tuple_and_string_array(self):
        positions = [
            item for item in definitions("pay_to_play")
            if item["type"] == "function" and item["name"] == "viewPosition"
        ]
        self.assertEqual(len(positions), 1)
        position = positions[0]
        self.assertEqual(position["stateMutability"], "view")
        self.assertEqual(
            tuple((item["name"], item["type"]) for item in position["outputs"][0]["components"]),
            POSITION_FIELDS,
        )
        self.assertEqual(decode((POSITION_ABI_TYPE,), POSITION_RETURN), (POSITION_SAMPLE,))
        self.assertEqual(encode((POSITION_ABI_TYPE,), (POSITION_SAMPLE,)), POSITION_RETURN)
        self.assertEqual(POSITION_CALL[:4], keccak(text=POSITION_SIGNATURE)[:4])
        self.assertEqual(
            decode(("address", "uint256", "address"), POSITION_CALL[4:]),
            (POSITION_SAMPLE[0], (1 << 256) - 1, POSITION_SAMPLE[2]),
        )

    def test_fixture_catalogs_preserve_transfer_indexed_distinction(self):
        erc20 = definitions("erc20")[0]
        erc721 = definitions("erc721")[0]
        self.assertEqual((erc20["name"], erc721["name"]), ("Transfer", "Transfer"))
        self.assertEqual(
            tuple(entry["indexed"] for entry in erc20["inputs"]),
            (True, True, False),
        )
        self.assertEqual(
            tuple(entry["indexed"] for entry in erc721["inputs"]),
            (True, True, True),
        )
        self.assertEqual(
            {item["name"] for item in definitions("pay_to_play") if item["type"] == "function"},
            {"asset", "viewPosition"},
        )
        for entry in EVENT_VECTORS + EXTRA_EVENT_VECTORS:
            with self.subTest(name=entry.name):
                self.assertEqual(entry.topics[0], keccak(text={
                    "erc20": "Transfer(address,address,uint256)",
                    "erc721": "Transfer(address,address,uint256)",
                    "mixed": "Mixed(uint8,int16,bytes4,string)",
                    "metadata": "Metadata(string,bytes)",
                    "payment": VOUCHER_SIGNATURE,
                    "compound": COMPOUND_SIGNATURE,
                }[entry.contract]))
                self.assertEqual(len(entry.topics), 1 + sum(field["indexed"] for field in entry.abi["inputs"]))
                self.assertEqual(len(entry.data) % 32, 0)

    def test_abi_type_matrix_is_complete_and_explicit(self):
        matrix = json.loads((FIXTURES / "support_matrix.json").read_text(encoding="utf-8"))
        self.assertEqual(matrix["schema"], "conix.abi.support.v1")
        rows = matrix["families"]
        self.assertEqual(
            {row["family"] for row in rows},
            {"uintM", "intM", "address", "bool", "bytesN", "bytes", "string", "array", "tuple"},
        )
        self.assertEqual(len(rows), len({row["family"] for row in rows}))
        for row in rows:
            with self.subTest(family=row["family"]):
                self.assertIn(row["indexed_event"], ("scalar", "hash"))
                self.assertIs(row["nonindexed_event"], True)
                self.assertIs(row["call_input"], True)
                self.assertIs(row["call_output"], True)
                self.assertIs(type(row["indexed_equality"]), bool)
                self.assertEqual(row["indexed_equality"], row["indexed_event"] == "scalar")
                self.assertTrue(row["python"])
        rejection_cases = {row["case"] for row in matrix["rejected"]}
        self.assertTrue({
            "anonymous event", "tuple without components",
            "filter on nonindexed/dynamic/hashed argument",
            "noncanonical values", "caller type mismatch",
        } <= rejection_cases)


if __name__ == "__main__":
    unittest.main()
