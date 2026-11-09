"""RPC wire-type and envelope regressions for present and future ABI consumers.

Run through the real RpcClient with an in-memory HTTP transport, so JSON
parsing, response admission and typed helpers execute together.
"""
import json
import unittest

import httpx

from conix.rpc import RpcClient, RpcError, RpcProtocolError, data, quantity, read_block


class RpcScalarTypeContracts(unittest.TestCase):

    def test_canonical_uint256_hex_quantities_include_all_high_boundaries(self):
        values = (
            0, 1, 32768, 65535, 1 << 31, (1 << 32) - 1,
            1 << 63, (1 << 64) - 1, 1 << 128, 1 << 255, (1 << 256) - 1,
        )
        for expected in values:
            with self.subTest(expected=expected):
                self.assertEqual(quantity(hex(expected), "test"), expected)
                self.assertIs(type(quantity(hex(expected), "test")), int)

    def test_bad_quantity_types_widths_and_encodings_fail(self):
        bad = (
            None, False, True, 0, -1, 1.0, 1 << 256, [], {},
            "", "0x", "0X1", "0x00", "0x01", "0x-1", "0xg",
            "0xF", "0xffz", "0x" + "f" * 65, "1", "1e10",
        )
        for value in bad:
            with self.subTest(value=repr(value)[:100]), self.assertRaises(RpcProtocolError):
                quantity(value, "test")

    def test_bytes_are_not_numbers_and_logical_zero_bytes_are_retained(self):
        for value in (b"", b"\x00", b"\x00\xff", b"\xff\x00" * 16, bytes(range(32))):
            with self.subTest(length=len(value)):
                self.assertEqual(data("0x" + value.hex(), "test", size=len(value)), value)
                self.assertEqual(data("0x" + value.hex().upper(), "test", size=None), value)

        bad = (None, False, 0, 1.0, "0X00", "0x0", "0xgg", "0x123", [], {})
        for value in bad:
            with self.subTest(value=repr(value)), self.assertRaises(RpcProtocolError):
                data(value, "test", size=None)

        for value in (b"", bytes(31), bytes(33)):
            with self.subTest(length=len(value)), self.assertRaises(RpcProtocolError):
                data("0x" + value.hex(), "hash", size=32)

    def test_block_identity_preserves_all_word_bytes_and_uint_timestamp(self):
        block = read_block({
            "number": "0x" + format((1 << 63) - 1, "x"),
            "hash": "0x" + "00" * 31 + "ff",
            "parentHash": "0x" + "ff" * 31 + "00",
            "timestamp": "0x" + format((1 << 64) - 1, "x"),
        })
        self.assertEqual(block.number, (1 << 63) - 1)
        self.assertEqual(block.hash, bytes(31) + b"\xff")
        self.assertEqual(block.parent_hash, bytes.fromhex("ff" * 31 + "00"))
        self.assertEqual(block.timestamp, (1 << 64) - 1)

    def test_wrong_block_field_types_or_missing_identity_fail(self):
        canonical = {
            "number": "0x1",
            "hash": "0x" + "11" * 32,
            "parentHash": "0x" + "22" * 32,
            "timestamp": "0x5",
        }
        for changes in (
            {"number": 1},
            {"hash": "0x" + "11" * 31},
            {"parentHash": None},
            {"timestamp": 5.0},
            {"number": "0x00"},
        ):
            with self.subTest(changes=changes), self.assertRaises(RpcProtocolError):
                read_block({**canonical, **changes})
        for name in canonical:
            with self.subTest(missing=name), self.assertRaises(RpcProtocolError):
                read_block({key: value for key, value in canonical.items() if key != name})


class RpcEnvelopeTypeContracts(unittest.IsolatedAsyncioTestCase):

    async def _call(self, body, *, response_headers=None, limit=4096):
        if isinstance(body, str):
            body = body.encode("utf-8")

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=body,
                headers=response_headers or {},
            )
        )

        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            timeout=None,
        ) as client:
            source = RpcClient(
                client=client,
                url="http://example.invalid",
                timeout=2,
                max_response_bytes=limit,
            )
            return await source.call("eth_getLogs", [])

    async def test_full_precision_json_integer_and_bytes_payload(self):
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "quantity": (1 << 256) - 1,
                "bytes": "0x00ff00",
            },
        })
        response = await self._call(payload)
        self.assertEqual(response["quantity"], (1 << 256) - 1)
        self.assertIs(type(response["quantity"]), int)
        self.assertEqual(data(response["bytes"], "response bytes", size=3), b"\x00\xff\x00")

    async def test_duplicate_keys_nonfinite_values_and_float_overflow_fail(self):
        invalid = (
            '{"jsonrpc":"2.0","id":1,"result":{"amount":1,"amount":2}}',
            '{"jsonrpc":"2.0","id":1,"result":NaN}',
            '{"jsonrpc":"2.0","id":1,"result":Infinity}',
            '{"jsonrpc":"2.0","id":1,"result":1e9999}',
            '{"jsonrpc":"2.0","id":1,"id":1,"result":[]}',
            '{"jsonrpc":"2.0","id":true,"result":[]}',
            '{"jsonrpc":"2.0","id":1,"result":[],"error":{"code":1,"message":"ignored"}}',
        )
        for index, body in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(RpcProtocolError):
                await self._call(body)

    async def test_remote_error_is_typed_and_does_not_reveal_remote_prose(self):
        secret = "secret-do-not-echo"
        response = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": secret},
        })
        with self.assertRaises(RpcError) as raised:
            await self._call(response)
        self.assertEqual(raised.exception.code, -32000)
        self.assertNotIn(secret, str(raised.exception))

    async def test_oversized_or_compressed_responses_fail_without_decoding(self):
        envelope = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": "x" * 300,
        })
        with self.assertRaises(RpcProtocolError):
            await self._call(envelope, limit=64)

        with self.assertRaises(RpcProtocolError):
            await self._call(
                envelope,
                response_headers={"Content-Encoding": "gzip"},
            )

    async def test_malformed_or_missing_json_results_do_not_turn_into_empty_events(self):
        for body in (
            "",
            "[]",
            '{"jsonrpc":"2.0","id":1}',
            '{"jsonrpc":"2.0","id":1,"result":[]',
            '{"jsonrpc":"1.0","id":1,"result":[]}',
            '{"jsonrpc":"2.0","id":"1","result":[]}',
            '{"jsonrpc":"2.0","id":2,"result":[]}',
        ):
            with self.subTest(body=body[:40]), self.assertRaises(RpcProtocolError):
                await self._call(body)


if __name__ == "__main__":
    unittest.main()
