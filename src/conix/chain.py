"""Check canonical block evidence against one configured Ethereum RPC source."""

from conix.configuration import Settings
from conix.models import Block, ObservationProtocolError, ObservationUnavailable
from conix.rpc import RpcError, RpcProtocolError, RpcUnavailable, data, quantity, read_block


class Chain:
    """The configured RPC source is trusted; it does not prove contract semantics."""

    def __init__(self, contract, settings: Settings):
        self.contract = contract
        self.rpc = contract.rpc
        self.settings = settings

    async def assert_source(self):
        """Check chain ID and code presence, not the identity of the bytecode."""
        deployment = self.settings.deployment

        try:
            chain_id = quantity(await self.rpc.call("eth_chainId", []), "chain ID")
            if chain_id != deployment.chain_id:
                raise ObservationProtocolError(f"{chain_id = } differs from configured {deployment.chain_id = }.")

            code_response = await self.rpc.call("eth_getCode", [deployment.address, "latest"])
            code = data(code_response, "deployed code", size=None)
            if not code:
                raise ObservationProtocolError(f"No contract code at configured address {deployment.address}.")
        except RpcProtocolError as error:
            raise ObservationProtocolError(f"Invalid chain identity or contract code response: {error}") from error

    async def header(self, selector: int | str) -> Block:
        """Resolve a block number or supported tag, retaining its full identity."""
        if type(selector) is int:
            if not 0 <= selector < 2**63:
                raise ValueError("Block selector is outside PostgreSQL block-height limits.")

            argument = hex(selector)
        elif selector in ("latest", "finalized"):
            argument = selector
        else:
            raise ValueError("Unsupported block selector.")

        try:
            result = await self.rpc.call("eth_getBlockByNumber", [argument, False])
            if result is None:
                raise ObservationUnavailable(f"RPC has no block for selector {argument}.")

            block = read_block(result)
            if type(selector) is int and block.number != selector:
                raise ObservationProtocolError(f"RPC block-number request {selector} returned height {block.number}.")

            if block.number >= 2**63:
                raise ObservationProtocolError("RPC block height exceeds supported signed 64-bit storage.")

            return block
        except RpcProtocolError as error:
            raise ObservationProtocolError(f"Invalid RPC block response for {argument}: {error}") from error

    async def canonical(self, block: Block):
        candidate = await self.header(block.number)
        return candidate.hash == block.hash and candidate.parent_hash == block.parent_hash

    async def current_head(self) -> Block:
        return await self.header("latest")

    async def finalized(self) -> Block:
        try:
            return await self.header("finalized")
        except (RpcError, RpcUnavailable, RpcProtocolError) as error:
            raise ObservationUnavailable("Configured RPC source cannot establish a finalized block.") from error

    async def connect(self, lower: Block, upper: Block):
        """Check every parent link when finality extends beyond retained history."""
        if lower.number > upper.number:
            raise ValueError("Ancestry endpoints must be ordered.")

        if upper.number - lower.number + 1 > self.settings.max_evidence_blocks:
            raise ObservationUnavailable("Finality ancestry exceeds max_evidence_blocks.")

        previous = lower
        for height in range(lower.number + 1, upper.number + 1):
            block = await self.header(height)
            if block.parent_hash != previous.hash:
                raise ObservationUnavailable(
                    f"Canonical ancestry is inconsistent between heights {height - 1} and {height}."
                )

            previous = block

        if previous.hash != upper.hash:
            raise ObservationUnavailable("The supplied upper block is not connected to the checkpoint.")
