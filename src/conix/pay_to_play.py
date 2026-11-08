# Adapted from numpde/pay-to-play@2a1adb00b2eba9a18b49c92ca39a69ecf59bb110
# src/local_observer/pay_to_play.py; shared protocol/ABI semantics.
"""Interpret the supported PayToPlay ABI; transport and history live elsewhere.

Profile source: dapp32/dapps/pay-to-play/src/PayToPlay.sol and its exported ABI.
The profile test compares these signatures/types with that authoritative ABI.
The caller supplies a trusted deployment; a decodable reply alone does not prove
that an arbitrary contract implements these financial semantics.
"""

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_typing import ChecksumAddress
from eth_utils import is_checksum_address, keccak, to_checksum_address

from conix.models import Block, Collection, Deployment, Phase, Position
from conix.rpc import RpcClient, RpcProtocolError, data, quantity

# The fixed ABI order includes actor-dependent actions, which are not returned
# as accounting facts. These are protocol definitions, not runtime settings.
# repo-check: allow-module-constant
POSITION_TYPES = (
    "address",
    "uint256",
    "address",
    "uint64",
    "uint256",
    "address",
    "address",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "address",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "address",
    "uint8",
    "string[]",
)
# repo-check: allow-module-constant
POSITION_SIGNATURE = "viewPosition(address,uint256,address)"
# repo-check: allow-module-constant
COLLECTION_SIGNATURE = "VoucherCollected(address,uint256,address,uint256,uint256,bytes32)"


def _abi(types, raw, label):
    """Reject malformed or trailing ABI bytes rather than project a partial reply."""
    try:
        values = decode(types, raw, strict=True)
        if encode(types, values) != raw:
            raise RpcProtocolError(f"PayToPlay {label} returned noncanonical or trailing ABI bytes.")
    except (DecodingError, ValueError, OverflowError) as error:
        raise RpcProtocolError(f"PayToPlay {label} returned malformed ABI bytes.") from error
    return values


class PayToPlay:
    """Read authoritative views and decode transfers for one trusted deployment."""

    def __init__(self, rpc: RpcClient, deployment: Deployment):
        if type(deployment.chain_id) is not int or not 0 <= deployment.chain_id < 2**256:
            raise ValueError("Deployment chain ID must fit uint256, excluding bool.")
        if not is_checksum_address(deployment.address):
            raise ValueError("Deployment contract address must be a checksummed EVM address.")
        self.rpc = rpc
        self.deployment = deployment

    async def _call(self, calldata: bytes, block: Block) -> bytes:
        result = await self.rpc.call(
            "eth_call",
            [
                {"to": self.deployment.address, "data": "0x" + calldata.hex()},
                {"blockHash": "0x" + block.hash.hex(), "requireCanonical": True},
            ],
        )
        return data(result, "contract return data", size=None)

    async def position(self, customer: ChecksumAddress, channel_id: int, block: Block) -> tuple[ChecksumAddress, Position]:
        """Read asset and position at the supplied block; actor zero supplies no UI identity."""
        if not is_checksum_address(customer):
            raise ValueError("Customer address must be a checksummed EVM address.")
        asset_raw = await self._call(keccak(text="asset()")[:4], block)
        asset = to_checksum_address(_abi(("address",), asset_raw, "asset")[0])
        calldata = keccak(text=POSITION_SIGNATURE)[:4] + encode(
            ("address", "uint256", "address"), (customer, channel_id, "0x0000000000000000000000000000000000000000")
        )
        result = await self._call(calldata, block)
        values = _abi(("(" + ",".join(POSITION_TYPES) + ")",), result, "position")[0]
        (
            returned_customer,
            returned_channel,
            payee,
            claim_period,
            channel_count,
            admin,
            collector,
            balance,
            voucher_paid,
            requested_amount,
            ready_at,
            effective_balance,
            voucher_key,
            scheduled_refund_at,
            refund_at,
            settle_at,
            close_at,
            escrow_total,
            refund_recipient,
            phase,
            _actions,
        ) = values
        # Named projection is independent of the dataclass's declaration order.
        # The actor-dependent UI action list has no accounting authority.
        try:
            phase = Phase(phase)
        except ValueError as error:
            raise RpcProtocolError("PayToPlay position returned an unsupported phase.") from error
        position = Position(
            customer=to_checksum_address(returned_customer),
            channel_id=returned_channel,
            payee=to_checksum_address(payee),
            claim_period=claim_period,
            channel_count=channel_count,
            admin=to_checksum_address(admin),
            collector=to_checksum_address(collector),
            balance=balance,
            voucher_paid=voucher_paid,
            requested_amount=requested_amount,
            ready_at=ready_at,
            effective_balance=effective_balance,
            voucher_key=to_checksum_address(voucher_key),
            scheduled_refund_at=scheduled_refund_at,
            refund_at=refund_at,
            settle_at=settle_at,
            close_at=close_at,
            escrow_total=escrow_total,
            refund_recipient=to_checksum_address(refund_recipient),
            phase=phase,
        )
        if position.customer != customer or position.channel_id != channel_id:
            raise RpcProtocolError("PayToPlay returned another customer or channel's position.")
        return asset, position

    async def voucher_collected_events(self, block: Block) -> tuple[Collection, ...]:
        """Read this contract's positive collection events pinned to one block hash."""
        result = await self.rpc.call(
            "eth_getLogs",
            [
                {
                    "address": self.deployment.address,
                    "blockHash": "0x" + block.hash.hex(),
                    "topics": ["0x" + keccak(text=COLLECTION_SIGNATURE).hex()],
                }
            ],
        )
        if type(result) is not list:
            raise RpcProtocolError("RPC collection log result must be an array.")
        return tuple(sorted((self.collection(item, block) for item in result), key=lambda event: event.log_index))

    def collection(self, value: object, block: Block) -> Collection:
        """Decode one positive transfer occurrence; its invoice reference is not allocation."""
        if type(value) is not dict:
            raise RpcProtocolError("RPC collection log must be an object.")
        try:
            topics = value["topics"]
            if type(topics) is not list or len(topics) != 4:
                raise RpcProtocolError("PayToPlay collection log must have four topics.")
            decoded_topics = [data(topic, "event topic", size=32) for topic in topics]
            if decoded_topics[0] != keccak(text=COLLECTION_SIGNATURE):
                raise RpcProtocolError("RPC returned a different event in the collection query.")
            if (
                value["removed"] is not False
                or to_checksum_address(data(value["address"], "log address", size=20)) != self.deployment.address
            ):
                raise RpcProtocolError("RPC returned a removed log or another contract's log.")
            if (
                quantity(value["blockNumber"], "log block") != block.number
                or data(value["blockHash"], "log block hash", size=32) != block.hash
            ):
                raise RpcProtocolError("RPC returned a collection from another block.")
            customer, channel, payee = _abi(("address", "uint256", "address"), b"".join(decoded_topics[1:]), "collection topics")
            amount, paid, invoice_hash = _abi(
                ("uint256", "uint256", "bytes32"), data(value["data"], "log data", size=96), "collection"
            )
            if amount == 0 or amount > paid:
                raise RpcProtocolError("PayToPlay collection log has inconsistent positive payment amounts.")
            return Collection(
                block.number,
                block.hash,
                block.timestamp,
                data(value["transactionHash"], "transaction hash", size=32),
                quantity(value["transactionIndex"], "transaction index"),
                quantity(value["logIndex"], "log index"),
                to_checksum_address(customer),
                channel,
                to_checksum_address(payee),
                amount,
                paid,
                invoice_hash,
            )
        except KeyError as error:
            raise RpcProtocolError("RPC collection log is missing a required field.") from error
