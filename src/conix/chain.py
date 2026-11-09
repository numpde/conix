"""Check canonical block evidence against one configured Ethereum RPC source."""

from conix.configuration import Settings
from conix.models import Block, ObservationProtocolError, ObservationUnavailable
from conix.rpc import RpcError, RpcProtocolError, RpcUnavailable, data, quantity, read_block


class Chain:
    """Trust one configured RPC source without assuming contract bytecode identity."""

    def __init__(self, contract, settings: Settings):
        self.contract = contract
        self.rpc = contract.rpc
        self.settings = settings

    async def assert_source(self):
        """Check chain ID and code presence; neither verifies contract semantics."""
        deployment = self.settings.deployment

        try:
            expected_chain_id = deployment.chain_id
            actual_chain_id = quantity(await self.rpc.call("eth_chainId", []), "chain ID")

            if actual_chain_id != expected_chain_id:
                raise ObservationProtocolError(f"{actual_chain_id = }, {expected_chain_id = }")

            contract_address = deployment.address

            contract_code = data(
                value=(await self.rpc.call("eth_getCode", [contract_address, "latest"])),
                label="contract code",
                size=None,
            )

            if not contract_code:
                raise ObservationProtocolError(f"No contract code at {contract_address = }")
        except RpcProtocolError as error:
            raise ObservationProtocolError(f"Invalid chain identity or contract code response: {error}") from error

    async def header(self, selector: int | str) -> Block:
        """Resolve a block number or tag and retain the full returned identity."""
        if type(selector) is int:
            if not 0 <= selector < 2**63:
                raise ValueError("Block selector is outside PostgreSQL block-height limits.")

            rpc_selector = hex(selector)
        elif selector in ("latest", "finalized"):
            rpc_selector = selector
        else:
            raise ValueError("Unsupported block selector.")

        try:
            block_response = await self.rpc.call("eth_getBlockByNumber", [rpc_selector, False])
            if block_response is None:
                raise ObservationUnavailable(f"RPC has no block for {rpc_selector = }.")

            resolved_block = read_block(block_response)
            if type(selector) is int and resolved_block.number != selector:
                raise ObservationProtocolError(f"Requested {selector = }, returned {resolved_block.number = }.")

            if resolved_block.number >= 2**63:
                raise ObservationProtocolError("RPC block height exceeds supported signed 64-bit storage.")

            return resolved_block
        except RpcProtocolError as error:
            raise ObservationProtocolError(f"Invalid RPC block response for {rpc_selector = }: {error}") from error

    async def canonical(self, block: Block):
        canonical_block = await self.header(block.number)
        return canonical_block.hash == block.hash and canonical_block.parent_hash == block.parent_hash

    async def current_head(self) -> Block:
        return await self.header("latest")

    async def finalized(self) -> Block:
        try:
            return await self.header("finalized")
        except (RpcError, RpcUnavailable, RpcProtocolError) as error:
            raise ObservationUnavailable("Configured RPC source cannot establish a finalized block.") from error

    async def connect(self, lower: Block, upper: Block):
        """Follow every parent link when finality extends beyond retained history."""
        if lower.number > upper.number:
            raise ValueError("Ancestry endpoints must be ordered.")

        if upper.number - lower.number + 1 > self.settings.max_evidence_blocks:
            raise ObservationUnavailable("Finality ancestry exceeds max_evidence_blocks.")

        previous_block = lower

        for block_number in range(lower.number + 1, upper.number + 1):
            canonical_block = await self.header(block_number)
            if canonical_block.parent_hash != previous_block.hash:
                raise ObservationUnavailable(
                    f"Canonical ancestry is inconsistent between heights {block_number - 1} and {block_number}."
                )

            previous_block = canonical_block

        if previous_block.hash != upper.hash:
            raise ObservationUnavailable("The supplied upper block is not connected to the checkpoint.")
