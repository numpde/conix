"""Independent ABI vectors and adverse caller/wire tests for the generic codec."""

import json
from pathlib import Path
import random
import unittest

from eth_abi import encode as reference_encode
from eth_utils import to_checksum_address

from abi_golden_vectors import ERC20, ERC721, MIXED, HASHED, FROM, TO, word
from abi_contract_vectors import (
    COMPOUND, PAY_TO_PLAY, POSITION_ABI_TYPE, POSITION_CALL,
    POSITION_RETURN, POSITION_SAMPLE,
)
from conix.abi import (
    AbiLimits, IndexedHash, InvalidCallerValue, MalformedABIData,
    ResourceExceeded, UnsupportedABI, compile_contract, compile_event,
    compile_function, decode_event, decode_return, encode_call,
    encode_indexed_filter,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "abi"


def catalog(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def scalar_event(kind, *, indexed=True, name="n"):
    return compile_event({
        "type": "event", "name": "Value", "anonymous": False,
        "inputs": [{"name": name, "type": kind, "indexed": indexed}],
    })


class ExactEventContracts(unittest.TestCase):

    def test_erc20_erc721_compound_pay_to_play_and_mixed_vectors(self):
        for record in (ERC20, ERC721, MIXED, PAY_TO_PLAY, COMPOUND):
            with self.subTest(record=record.name):
                event = compile_event(record.abi)
                self.assertEqual(event.signature, {
                    "erc20": "Transfer(address,address,uint256)",
                    "erc721": "Transfer(address,address,uint256)",
                    "mixed": "Mixed(uint8,int16,bytes4,string)",
                    "payment": "VoucherCollected(address,uint256,address,uint256,uint256,bytes32)",
                    "compound": "Complex(address,(uint16,bytes8)[],string[],bytes)",
                }[record.contract])
                self.assertEqual(event.topic0, record.topics[0])
                self.assertEqual(decode_event(event, record.topics, record.data), record.expected)

        erc20, erc721 = compile_event(ERC20.abi), compile_event(ERC721.abi)
        self.assertEqual(erc20.topic0, erc721.topic0)
        self.assertNotEqual(erc20.arguments, erc721.arguments)
        self.assertEqual(erc20.indexed_count, 2)
        self.assertEqual(erc721.indexed_count, 3)

    def test_hashed_dynamic_field_is_not_a_preimage(self):
        event = compile_event(HASHED.abi)
        result = decode_event(event, HASHED.topics, HASHED.data)
        self.assertEqual(result, (IndexedHash(HASHED.topics[1]), HASHED.expected[1]))
        self.assertNotEqual(result[0], "café")
        self.assertEqual(result[0].topic, HASHED.topics[1])
        with self.assertRaises(InvalidCallerValue):
            encode_indexed_filter(event, "indexedText", "café")
        with self.assertRaises(MalformedABIData):
            IndexedHash(b"\x00" * 31)

    def test_all_supported_integer_widths_in_exact_topics(self):
        for width in range(8, 257, 8):
            for kind in ("uint", "int"):
                event = scalar_event(f"{kind}{width}")
                if kind == "uint":
                    values = (0, 1, (1 << (width - 1)) - 1,
                              1 << (width - 1), (1 << width) - 1)
                else:
                    values = (-(1 << (width - 1)), -1, 0, 1, (1 << (width - 1)) - 1)
                for number in values:
                    with self.subTest(kind=kind, width=width, number=number):
                        topic = encode_indexed_filter(event, "n", number)
                        self.assertEqual(topic, reference_encode((f"{kind}{width}",), (number,)))
                        self.assertEqual(decode_event(event, (event.topic0, topic), b""), (number,))
                for invalid in (True, 1.5, str(values[0])):
                    with self.subTest(kind=kind, width=width, bad=repr(invalid)):
                        with self.assertRaises(InvalidCallerValue):
                            encode_indexed_filter(event, "n", invalid)

                invalid = (1 << width) if kind == "uint" else -(1 << (width - 1)) - 1
                with self.assertRaises(InvalidCallerValue):
                    encode_indexed_filter(event, "n", invalid)

    def test_bytes_widths_and_bool_without_coercion(self):
        for width in range(1, 33):
            event = scalar_event(f"bytes{width}")
            value = b"\x00" * (width - 1) + b"\xff"
            topic = encode_indexed_filter(event, "n", value)
            self.assertEqual(topic, value + bytes(32 - width))
            self.assertEqual(decode_event(event, (event.topic0, topic), b""), (value,))
            with self.assertRaises(InvalidCallerValue):
                encode_indexed_filter(event, "n", value + b"\x00")
            with self.assertRaises(InvalidCallerValue):
                encode_indexed_filter(event, "n", "0x" + value.hex())
            if width < 32:
                with self.assertRaises(MalformedABIData):
                    decode_event(event, (event.topic0, topic[:-1] + b"\x01"), b"")

        event = scalar_event("bool")
        self.assertEqual(encode_indexed_filter(event, "n", True), word(1))
        self.assertEqual(decode_event(event, (event.topic0, word(1)), b""), (True,))
        for invalid in (0, 1, "false"):
            with self.assertRaises(InvalidCallerValue):
                encode_indexed_filter(event, "n", invalid)
        with self.assertRaises(MalformedABIData):
            decode_event(event, (event.topic0, word(2)), b"")

    def test_address_presentation_and_checksum_validation(self):
        event = scalar_event("address")
        self.assertEqual(encode_indexed_filter(event, "n", TO), bytes(12) + bytes.fromhex(TO[2:]))
        self.assertEqual(decode_event(event, (event.topic0, bytes(12) + bytes.fromhex(TO[2:])), b""), (TO,))
        lowercase = TO.lower()
        self.assertEqual(encode_indexed_filter(event, "n", lowercase), bytes(12) + bytes.fromhex(TO[2:]))
        with self.assertRaises(InvalidCallerValue):
            encode_indexed_filter(event, "n", "0x" + TO[2:].swapcase())

    def test_wrong_topic_count_topic0_and_noncanonical_data_fail(self):
        event = compile_event(ERC20.abi)
        bad_logs = (
            (ERC20.topics[:2], ERC20.data),
            (ERC20.topics + (word(0),), ERC20.data),
            ((word(0),) + ERC20.topics[1:], ERC20.data),
            (ERC20.topics, ERC20.data + word(0)),
            (ERC20.topics, ERC20.data[:-1]),
        )
        for topics, data in bad_logs:
            with self.subTest(topics=len(topics), data=len(data)):
                with self.assertRaises(MalformedABIData):
                    decode_event(event, topics, data)

    def test_noncanoical_signed_padding_and_unsigned_overflow(self):
        unsigned = scalar_event("uint8")
        signed = scalar_event("int16")
        with self.assertRaises(MalformedABIData):
            decode_event(unsigned, (unsigned.topic0, word(256)), b"")
        with self.assertRaises(MalformedABIData):
            decode_event(signed, (signed.topic0, word(0x8000)), b"")

    def test_indexed_filter_name_and_field_position(self):
        event = compile_event(PAY_TO_PLAY.abi)
        maximum = (1 << 256) - 1
        self.assertEqual(encode_indexed_filter(event, "channelId", maximum), word(maximum))
        self.assertEqual(event.arguments[1].topic_index, 2)
        for field in ("amount", "invoiceHash", "missing", ""):
            with self.subTest(field=field), self.assertRaises(InvalidCallerValue):
                encode_indexed_filter(event, field, 0)

        duplicate = {
            "type": "event", "name": "D", "anonymous": False,
            "inputs": [
                {"name": "x", "type": "uint256", "indexed": True},
                {"name": "x", "type": "address", "indexed": True},
            ],
        }
        event = compile_event(duplicate)
        self.assertEqual((event.arguments[0].ordinal, event.arguments[1].ordinal), (0, 1))
        with self.assertRaises(InvalidCallerValue):
            encode_indexed_filter(event, "x", 1)

    def test_hostile_nested_offsets_and_array_length_bombs(self):
        event = compile_event(COMPOUND.abi)
        attacks = (
            word(32) + COMPOUND.data[32:],             # pointer into tuple head
            COMPOUND.data[:256] + word(1 << 255) + COMPOUND.data[288:],
            COMPOUND.data[:96] + word(1 << 255) + COMPOUND.data[128:],
            COMPOUND.data[:448] + word(1 << 255) + COMPOUND.data[480:],
            COMPOUND.data + word(0),                   # trailing bytes
            COMPOUND.data[:-32],                       # truncated dynamic value
        )
        for modified in attacks:
            with self.subTest(nbytes=len(modified)):
                with self.assertRaises((MalformedABIData, ResourceExceeded)):
                    decode_event(event, COMPOUND.topics, modified)
        with self.assertRaises(ResourceExceeded):
            decode_event(event, COMPOUND.topics, COMPOUND.data, limits=AbiLimits(max_elements=1))

    def test_seeded_real_encoded_nested_values(self):
        kind = {
            "type": "event", "name": "Nested", "anonymous": False,
            "inputs": [{"name": "values", "type": "tuple[]", "indexed": False,
                        "components": [{"name": "n", "type": "uint16"},
                                       {"name": "s", "type": "string"}]}],
        }
        event = compile_event(kind)
        for seed in range(40):
            source = random.Random(seed)
            values = tuple((source.randrange(0, 65536), source.choice(("", "é", "ok", "😀")))
                           for _ in range(source.randrange(0, 5)))
            raw = reference_encode(("(uint16,string)[]",), (values,))
            with self.subTest(seed=seed):
                self.assertEqual(decode_event(event, (event.topic0,), raw), (values,))


class ReadOnlyFunctionContracts(unittest.TestCase):

    def test_actual_view_position_tuple_and_function_selector(self):
        source = next(row for row in catalog("pay_to_play") if row.get("name") == "viewPosition")
        descriptor = compile_function(source)
        self.assertEqual(descriptor.signature, "viewPosition(address,uint256,address)")
        self.assertEqual(encode_call(descriptor, (FROM, (1 << 256) - 1, TO)), POSITION_CALL)
        self.assertEqual(decode_return(descriptor, POSITION_RETURN), (POSITION_SAMPLE,))
        self.assertEqual(descriptor.outputs[0].type.canonical, POSITION_ABI_TYPE)

    def test_tuple_array_call_and_nested_return(self):
        descriptor = compile_function(next(row for row in catalog("compound") if row.get("name") == "echo"))
        values = ((3, b"abcdefgh"), (65535, b"\x00" * 8))
        raw = reference_encode(("(uint16,bytes8)[]",), (values,))
        call = encode_call(descriptor, (values,))
        self.assertEqual(call[:4], descriptor.selector)
        self.assertEqual(call[4:], raw)
        self.assertEqual(decode_return(descriptor, raw), (values,))
        for invalid in ([values], (list(values),), (values[0],), ((3, b"x"),)):
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(InvalidCallerValue):
                    encode_call(descriptor, invalid)

    def test_view_pure_only_and_return_canonicality(self):
        method = {"type": "function", "name": "do", "stateMutability": "nonpayable",
                  "inputs": [], "outputs": []}
        with self.assertRaises(UnsupportedABI):
            compile_function(method)
        view = compile_function({**method, "stateMutability": "view"})
        self.assertEqual(encode_call(view, ()), view.selector)
        self.assertEqual(decode_return(view, b""), ())
        with self.assertRaises(MalformedABIData):
            decode_return(view, word(0))
        with self.assertRaises(InvalidCallerValue):
            encode_call(view, ("extra",))


class AbiAdmissionContracts(unittest.TestCase):

    def test_unsupported_types_and_missing_tuple_shapes_refused(self):
        for name in ("function", "fixed128x18", "ufixed128x18",
                     "bytes33", "uint7", "int0", "uint256[0]"):
            with self.subTest(name=name), self.assertRaises((UnsupportedABI, ResourceExceeded)):
                scalar_event(name)
        with self.assertRaises(UnsupportedABI):
            scalar_event("tuple")
        for entry in (
            {"type": "event", "name": "Bad", "anonymous": True, "inputs": []},
            {"type": "event", "name": "Bad", "inputs": []},
            {"type": "event", "name": "Bad", "anonymous": False,
             "inputs": [{"name": "i", "type": "uint256"}]},
        ):
            with self.subTest(entry=entry), self.assertRaises(UnsupportedABI):
                compile_event(entry)

    def test_contract_resolves_exact_signatures_and_rejects_ambiguous_layout(self):
        erc20 = compile_contract(catalog("erc20"), ("Transfer(address,address,uint256)",))
        erc721 = compile_contract(catalog("erc721"), ("Transfer(address,address,uint256)",))
        self.assertEqual(erc20.events[0].indexed_count, 2)
        self.assertEqual(erc721.events[0].indexed_count, 3)
        self.assertEqual(erc20.functions[0].signature, "balanceOf(address)")
        with self.assertRaises(UnsupportedABI):
            compile_contract(catalog("erc20"), ("Unselected()",))
        with self.assertRaises(UnsupportedABI):
            compile_contract(catalog("erc20") + catalog("erc721"), ("Transfer(address,address,uint256)",))
        with self.assertRaises(UnsupportedABI):
            compile_contract(catalog("erc20"), ("Transfer(address,address,uint256)",) * 2)

    def test_nesting_and_payload_limits_are_hard_bounds(self):
        with self.assertRaises(ResourceExceeded):
            AbiLimits(max_nesting=9)
        with self.assertRaises(ResourceExceeded):
            AbiLimits(max_bytes=1048577)
        with self.assertRaises(ResourceExceeded):
            scalar_event("uint16[4097]")
        with self.assertRaises(ResourceExceeded):
            compile_event(COMPOUND.abi, limits=AbiLimits(max_nesting=1))
        with self.assertRaises(ResourceExceeded):
            decode_event(compile_event(ERC20.abi), ERC20.topics, ERC20.data,
                         limits=AbiLimits(max_bytes=16))


if __name__ == "__main__":
    unittest.main()
