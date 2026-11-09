# Adapted from numpde/pay-to-play@2a1adb00b2eba9a18b49c92ca39a69ecf59bb110
# src/local_observer/pay_to_play.py; shared protocol/ABI semantics.
"""Decode the supported PayToPlay ABI; transport and history live elsewhere.

Profile source: dapp32/dapps/pay-to-play/src/PayToPlay.sol and its exported ABI.
The caller selects a trusted deployment: decodable replies do not establish
that arbitrary bytecode implements the expected financial semantics.
"""

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_typing import ChecksumAddress
from eth_utils import is_checksum_address, keccak, to_checksum_address

from conix.models import Block, Collection, Deployment, Phase, Position
from conix.rpc import RpcClient, RpcProtocolError, data, quantity

# Exact ABI order includes UI action fields, which have no accounting authority.
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


def _abi(abi_types, encoded_data, response_label):
    """Reject malformed, noncanonical and trailing ABI bytes."""
    try:
        decoded_values = decode(abi_types, encoded_data, strict=True)

        # Decoding alone can admit noncanonical padding or trailing bytes.
        if encode(abi_types, decoded_values) != encoded_data:
            raise RpcProtocolError(f"PayToPlay {response_label} returned noncanonical or trailing ABI bytes.")
    except (DecodingError, ValueError, OverflowError) as error:
        raise RpcProtocolError(f"PayToPlay {response_label} returned malformed ABI bytes.") from error

    return decoded_values


class PayToPlay:
    """Read contract views and decode collections for one trusted deployment."""

    def __init__(self, rpc: RpcClient, deployment: Deployment):
        if type(deployment.chain_id) is not int or not 0 <= deployment.chain_id < 2**256:
            raise ValueError("Deployment chain ID must fit uint256, excluding bool.")

        if not is_checksum_address(deployment.address):
            raise ValueError("Deployment contract address must be a checksummed EVM address.")

        self.rpc = rpc
        self.deployment = deployment

    async def _call(self, calldata: bytes, block: Block) -> bytes:
        """Pin a contract read to its canonical block hash, never 'latest'."""
        encoded_result = await self.rpc.call(
            method="eth_call",
            params=[
                {"to": self.deployment.address, "data": "0x" + calldata.hex()},
                {"blockHash": "0x" + block.hash.hex(), "requireCanonical": True},
            ],
        )

        return data(encoded_result, "contract return data", size=None)

    async def position(self, customer: ChecksumAddress, channel_id: int, block: Block) -> tuple[ChecksumAddress, Position]:
        """Read asset and position at one block; actor zero excludes UI identity."""
        if not is_checksum_address(customer):
            raise ValueError("Customer address must be a checksummed EVM address.")

        encoded_asset = await self._call(keccak(text="asset()")[:4], block)
        asset_address = to_checksum_address(_abi(("address",), encoded_asset, "asset")[0])

        # The actor-zero query reports contract state without UI-specific permissions.
        position_calldata = keccak(text=POSITION_SIGNATURE)[:4] + encode(
            ("address", "uint256", "address"),
            (customer, channel_id, "0x0000000000000000000000000000000000000000"),
        )

        encoded_position = await self._call(position_calldata, block)
        position_fields = _abi(("(" + ",".join(POSITION_TYPES) + ")",), encoded_position, "position")[0]

        (
            returned_customer,
            returned_channel_id,
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
            phase_code,
            _ui_actions,
        ) = position_fields

        try:
            phase = Phase(phase_code)
        except ValueError as error:
            raise RpcProtocolError("PayToPlay position returned an unsupported phase.") from error

        contract_position = Position(
            customer=to_checksum_address(returned_customer),
            channel_id=returned_channel_id,
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

        if contract_position.customer != customer or contract_position.channel_id != channel_id:
            raise RpcProtocolError("PayToPlay returned another customer or channel's position.")

        return asset_address, contract_position

    async def voucher_collected_events(self, block: Block) -> tuple[Collection, ...]:
        """Read positive collection occurrences pinned to the block hash."""
        rpc_logs = await self.rpc.call(
            method="eth_getLogs",
            params=[
                {
                    "address": self.deployment.address,
                    "blockHash": "0x" + block.hash.hex(),
                    "topics": ["0x" + keccak(text=COLLECTION_SIGNATURE).hex()],
                }
            ],
        )

        if type(rpc_logs) is not list:
            raise RpcProtocolError("RPC collection log result must be an array.")

        # Block-global log indices, not transaction hashes, order occurrences.
        return tuple(sorted((self.collection(log_entry, block) for log_entry in rpc_logs),
                            key=lambda collection: collection.log_index))

    def collection(self, value: object, block: Block) -> Collection:
        """Decode one transfer occurrence; invoice hash is not an allocation."""
        if type(value) is not dict:
            raise RpcProtocolError("RPC collection log must be an object.")

        try:
            encoded_topics = value["topics"]

            if type(encoded_topics) is not list or len(encoded_topics) != 4:
                raise RpcProtocolError("PayToPlay collection log must have four topics.")

            decoded_topics = [data(topic, "event topic", size=32) for topic in encoded_topics]

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

            (customer_address, channel_id, payee_address) = _abi(
                ("address", "uint256", "address"),
                b"".join(decoded_topics[1:]),
                "collection topics",
            )

            (collected_amount, cumulative_paid, invoice_hash) = _abi(
                ("uint256", "uint256", "bytes32"),
                data(value["data"], "log data", size=96),
                "collection",
            )

            if collected_amount == 0 or collected_amount > cumulative_paid:
                raise RpcProtocolError("PayToPlay collection log has inconsistent positive payment amounts.")

            return Collection(
                block_number=block.number,
                block_hash=block.hash,
                block_timestamp=block.timestamp,
                transaction_hash=data(value["transactionHash"], "transaction hash", size=32),
                transaction_index=quantity(value["transactionIndex"], "transaction index"),
                log_index=quantity(value["logIndex"], "log index"),
                customer=to_checksum_address(customer_address),
                channel_id=channel_id,
                payee=to_checksum_address(payee_address),
                amount=collected_amount,
                voucher_paid=cumulative_paid,
                invoice_hash=invoice_hash,
            )
        except KeyError as error:
            raise RpcProtocolError("RPC collection log is missing a required field.") from error
