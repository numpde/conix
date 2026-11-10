"""EVM test transport and independent receipt evidence for the T0-5 campaign.

This module belongs to the disposable fixture, not the installed Conix
distribution. Only the owned campaign invokes Anvil state-changing methods.
A receipt proves a particular mined transaction; Conix never supplies its
own expected raw-log bytes.
"""

from __future__ import annotations

import asyncio
import time

from eth_abi import encode
from eth_utils import keccak

from fixture_protocol import ScenarioFailure


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioFailure(message)


def calldata(signature: str, types: tuple[str, ...], values: tuple) -> str:
    """Encode transaction inputs independently of the Conix ABI codec."""
    return "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()


async def receipt(rpc, descriptor, tx_hash: str) -> dict:
    """Require a successful mined receipt within the fixture's work budget."""
    deadline = time.monotonic() + 15

    while time.monotonic() < deadline:
        observed = await rpc(descriptor, "eth_getTransactionReceipt", [tx_hash])
        if observed is not None:
            require(
                type(observed) is dict
                and observed.get("status") == "0x1"
                and observed.get("transactionHash", "").lower() == tx_hash.lower(),
                "The mined transaction receipt is unsuccessful or contradictory.",
            )
            return observed

        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))

    raise ScenarioFailure("A submitted test transaction has no mined receipt.")


async def send(
    rpc,
    descriptor,
    sender,
    signature,
    types,
    values,
    *,
    value=0,
    contract=None,
    wait=True,
):
    """Submit on the exclusively owned Anvil using a disposable test identity.

    Impersonation is deliberately test-only. It never establishes signature
    authority; the deployed PayToPlay verifies its vouchers independently.
    Cleanup of Anvil impersonation is attempted even if submission fails.
    """
    destination = contract or descriptor["contracts"]["PayToPlay"]["address"]

    await rpc(descriptor, "anvil_impersonateAccount", [sender])
    submission_error = None

    try:
        tx_hash = await rpc(descriptor, "eth_sendTransaction", [{
            "from": sender,
            "to": destination,
            "data": calldata(signature, types, values),
            "value": hex(value),
            "gas": hex(500000),
        }])
    except BaseException as error:
        submission_error = error
        raise
    finally:
        try:
            await rpc(descriptor, "anvil_stopImpersonatingAccount", [sender])
        except BaseException as retirement_error:
            if submission_error is None:
                raise
            submission_error.add_note(
                f"Anvil impersonation retirement failed ({type(retirement_error).__name__}); "
                "the fixture state requires inspection."
            )

    require(
        type(tx_hash) is str
        and tx_hash.startswith("0x")
        and len(tx_hash) == 66
        and all(ch in "0123456789abcdefABCDEF" for ch in tx_hash[2:]),
        "Anvil did not return a transaction hash.",
    )
    return await receipt(rpc, descriptor, tx_hash) if wait else tx_hash


def assert_event_matches_receipt(event, log):
    """Compare full original wire evidence, not merely ABI-decoded values."""
    require(
        event.transaction_hash.hex() == log["transactionHash"][2:].lower()
        and event.block.hash.hex() == log["blockHash"][2:].lower()
        and event.block.number == int(log["blockNumber"], 16)
        and event.transaction_index == int(log["transactionIndex"], 16)
        and event.log_index == int(log["logIndex"], 16)
        and tuple(word.hex() for word in event.topics)
        == tuple(word[2:].lower() for word in log["topics"])
        and event.data.hex() == log["data"][2:].lower(),
        "Conix event differs from its independent mined transaction receipt.",
    )
