"""ABI view return types, identity and exact block-hash pinning.

Runs against the current real PayToPlay reader with a controlled RPC wire
response; the independent full-chain contract fixture remains separate.
"""
import unittest
from dataclasses import replace

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from conix.models import Block, Deployment, Phase
from conix.pay_to_play import POSITION_SIGNATURE, POSITION_TYPES, PayToPlay
from conix.rpc import RpcProtocolError


CONTRACT = to_checksum_address(bytes.fromhex("11" * 20))
CUSTOMER = to_checksum_address(bytes.fromhex("00" * 19 + "ab"))
PAYEE = to_checksum_address(bytes.fromhex("fe" * 20))
ASSET = to_checksum_address(bytes.fromhex("12" * 20))
KEY = to_checksum_address(bytes.fromhex("34" * 20))
BLOCK = Block(54321, bytes.fromhex("a5" * 32), bytes.fromhex("b6" * 32), 1710000001)
MAX = (1 << 256) - 1


def position_values():
    return (
        CUSTOMER, MAX, PAYEE,
        (1 << 64) - 1,  # claimPeriod: uint64, not BIGINT
        MAX, CONTRACT, PAYEE,
        MAX, MAX, MAX, MAX, MAX,
        KEY, MAX, MAX, MAX, MAX, MAX,
        CUSTOMER, 2, ("", "café", "a\x00b"),
    )


class WireSource:
    def __init__(self, *, position=None, asset=None):
        self.position = position if position is not None else position_values()
        self.asset = ASSET if asset is None else asset
        self.requests = []

    async def call(self, method, params):
        self.requests.append((method, params))
        selector = params[0]["data"][:10]
        if selector == "0x" + keccak(text="asset()")[:4].hex():
            return "0x" + encode(("address",), (self.asset,)).hex()

        if selector == "0x" + keccak(text=POSITION_SIGNATURE)[:4].hex():
            return "0x" + encode(
                ("(" + ",".join(POSITION_TYPES) + ")",),
                (self.position,),
            ).hex()

        raise AssertionError("An unrecognized ABI selector was sent to the test RPC.")


class PositionAbiTypeContracts(unittest.IsolatedAsyncioTestCase):

    async def test_real_position_projection_retains_uint256_and_uint64(self):
        rpc = WireSource()
        contract = PayToPlay(rpc, Deployment(31337, CONTRACT))
        asset, position = await contract.position(CUSTOMER, MAX, BLOCK)

        self.assertEqual(asset, ASSET)
        self.assertEqual(position.customer, CUSTOMER)
        self.assertEqual(position.channel_id, MAX)
        self.assertEqual(position.claim_period, (1 << 64) - 1)
        self.assertEqual(position.channel_count, MAX)
        self.assertEqual(position.balance, MAX)
        self.assertEqual(position.voucher_paid, MAX)
        self.assertEqual(position.effective_balance, MAX)
        self.assertEqual(position.voucher_key, KEY)
        self.assertEqual(position.refund_at, MAX)
        self.assertEqual(position.phase, Phase.REFUND_ONLY)
        self.assertIs(type(position.channel_id), int)

        self.assertEqual(len(rpc.requests), 2)
        for method, params in rpc.requests:
            self.assertEqual(method, "eth_call")
            self.assertEqual(params[0]["to"], CONTRACT)
            self.assertEqual(
                params[1],
                {"blockHash": "0x" + BLOCK.hash.hex(), "requireCanonical": True},
            )

        expected_arguments = encode(
            ("address", "uint256", "address"),
            (CUSTOMER, MAX, "0x0000000000000000000000000000000000000000"),
        )
        self.assertEqual(
            rpc.requests[1][1][0]["data"],
            "0x" + (keccak(text=POSITION_SIGNATURE)[:4] + expected_arguments).hex(),
        )

    async def test_mismatched_position_identity_fails_instead_of_coercing(self):
        for index, wrong_value in (
            (0, PAYEE),
            (1, MAX - 1),
        ):
            changed = list(position_values())
            changed[index] = wrong_value
            contract = PayToPlay(
                WireSource(position=tuple(changed)),
                Deployment(31337, CONTRACT),
            )
            with self.subTest(field=index), self.assertRaises(RpcProtocolError):
                await contract.position(CUSTOMER, MAX, BLOCK)

    async def test_out_of_range_position_phase_fails_explicitly(self):
        changed = list(position_values())
        changed[-2] = 255
        contract = PayToPlay(
            WireSource(position=tuple(changed)),
            Deployment(31337, CONTRACT),
        )
        with self.assertRaises(RpcProtocolError):
            await contract.position(CUSTOMER, MAX, BLOCK)

    async def test_mutated_tuple_return_does_not_admit_missing_or_trailing_bytes(self):
        class MalformedSource(WireSource):
            def __init__(self, trailer):
                super().__init__()
                self.trailer = trailer

            async def call(self, method, params):
                encoded = await super().call(method, params)
                if params[0]["data"].startswith("0x" + keccak(text=POSITION_SIGNATURE)[:4].hex()):
                    return encoded + self.trailer
                return encoded

        for suffix in ("00", "00" * 32):
            with self.subTest(suffix_size=len(suffix)):
                contract = PayToPlay(
                    MalformedSource(suffix),
                    Deployment(31337, CONTRACT),
                )
                with self.assertRaises(RpcProtocolError):
                    await contract.position(CUSTOMER, MAX, BLOCK)


if __name__ == "__main__":
    unittest.main()
