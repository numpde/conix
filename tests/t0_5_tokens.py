"""Real deployed Solidity token fixtures for Conix's generic T0-5 acceptance.

The compiled creation bytes are checked against a retained Solidity source.
All deployments and state-changing calls target the explicitly owned Anvil test
chain. Conix's installed package is only used to index/query their results.
"""

from __future__ import annotations

from hashlib import sha1
import json
from pathlib import Path

from eth_abi import decode
from eth_utils import keccak, to_checksum_address

from abi_golden_vectors import TRANSFER_SIGNATURE as TRANSFER
from t0_5_chain import (
    assert_event_matches_receipt, calldata, require, send, submit_test_transaction,
    wait_for_receipt,
)

ZERO = "0x" + "00" * 20
ROOT = Path(__file__).resolve().parent / "fixtures" / "evm"


async def call_token_view(rpc, descriptor, contract: str, signature: str, types: tuple[str, ...],
               values: tuple, results: tuple[str, ...]):
    raw = await rpc(descriptor, "eth_call", [{
        "to": contract, "data": calldata(signature, types, values),
    }, "latest"])
    require(type(raw) is str and raw.startswith("0x"),
            "Deployed token did not return ABI-encoded data.")
    return decode(results, bytes.fromhex(raw[2:]))


def token_creation_bytecode() -> dict:
    """Require the source revision recorded with these precompiled fixtures.

    The Git blob identity detects source edits without artifact review. It
    does NOT prove a reproducible compiler output; that remains a separate
    qualification requirement before promoting these test artifacts.
    """
    source = (ROOT / "T0Tokens.sol").read_bytes()
    artifact = json.loads((ROOT / "token_bytecode.json").read_text(encoding="utf-8"))
    git_blob = b"blob " + str(len(source)).encode("ascii") + b"\x00" + source
    require(
        artifact["schema"] == "conix.test-tokens.v1"
        and sha1(git_blob).hexdigest() == artifact["sourceBlobSha"]
        and artifact["compiler"] == "solc-js 0.8.35"
        and artifact["compilerSettings"] == {
            "optimizer": {"enabled": True, "runs": 200}, "evmVersion": "prague",
        }
        and set(artifact["bytecode"]) == {"ConixToken20", "ConixToken721"},
        "Reviewed Solidity source and pinned creation artifacts disagree.",
    )
    for bytecode in artifact["bytecode"].values():
        require(type(bytecode) is str and bytecode.startswith("0x")
                and 0 < len(bytecode) <= 30000 and len(bytecode) % 2 == 0
                and all(char in "0123456789abcdef" for char in bytecode[2:]),
                "Reviewed token creation bytecode is malformed.")
    return artifact["bytecode"]


async def deploy_token_fixtures(descriptor, rpc) -> dict:
    """Deploy real ERC-20/721 test code before owner schema initialization."""
    bytecode = token_creation_bytecode()
    creator = descriptor["publicFixtureAccounts"]["admin"]["address"]
    contracts = {}
    for (name, alias) in (("ConixToken20", "token20"), ("ConixToken721", "token721")):
        receipt = await submit_test_transaction(
            rpc, descriptor, creator,
            {"data": bytecode[name], "gas": hex(3_000_000)},
        )
        tx_hash = receipt["transactionHash"]
        address = receipt.get("contractAddress")
        require(type(address) is str and address.startswith("0x") and len(address) == 42,
                "Deployed Solidity fixture has no contract address.")
        runtime = await rpc(descriptor, "eth_getCode", [address, "latest"])
        require(type(runtime) is str and len(runtime) > 2 and runtime != "0x",
                "Deployed Solidity fixture has no EVM runtime code.")
        contracts[alias] = {
            "address": address, "deploymentBlock": int(receipt["blockNumber"], 16),
            "creationTransaction": tx_hash,
        }

    token20 = contracts["token20"]["address"]
    token721 = contracts["token721"]["address"]
    require((await call_token_view(rpc, descriptor, token20, "balanceOf(address)",
                        ("address",), (creator,), ("uint256",))) == (1000,),
            "Real ERC-20 constructor did not allocate its initial balance.")
    require((await call_token_view(rpc, descriptor, token721, "ownerOf(uint256)",
                        ("uint256",), (7,), ("address",)))[0].lower() == creator.lower(),
            "Real ERC-721 constructor did not mint token 7 to the creator.")

    return {"schema": "conix.acceptance.tokens.v1", **contracts}


def only_transfer(receipt, contract):
    topic0 = "0x" + keccak(text=TRANSFER).hex()
    matching = [
        row for row in receipt["logs"]
        if row["address"].lower() == contract.lower()
        and row["topics"][0].lower() == topic0
    ]
    require(len(matching) == 1, "Token transaction did not emit one selected Transfer.")
    return matching[0]


async def emit_initial_transfers(rpc, descriptor, tokens):
    """Place meaningful transfers before the shared mixed-contract block."""
    admin = descriptor["publicFixtureAccounts"]["admin"]["address"]
    customer = descriptor["publicFixtureAccounts"]["customer"]["address"]
    transfer20 = await send(
        rpc, descriptor, admin, "transfer(address,uint256)",
        ("address", "uint256"), (customer, 100),
        contract=tokens["token20"]["address"],
    )
    transfer721 = await send(
        rpc, descriptor, admin, "transferFrom(address,address,uint256)",
        ("address", "address", "uint256"), (admin, customer, 7),
        contract=tokens["token721"]["address"],
    )
    return {"token20": transfer20, "token721": transfer721}


async def submit_pending_token_transfers(rpc, descriptor, tokens):
    """Submit two pending token transfers; the caller also submits PayToPlay."""
    admin = descriptor["publicFixtureAccounts"]["admin"]["address"]
    customer = descriptor["publicFixtureAccounts"]["customer"]["address"]
    relayer = descriptor["publicFixtureAccounts"]["relayer"]["address"]
    tx20 = await send(
        rpc, descriptor, admin, "transfer(address,uint256)",
        ("address", "uint256"), (relayer, 25),
        contract=tokens["token20"]["address"], wait=False,
    )
    tx721 = await send(
        rpc, descriptor, customer, "transferFrom(address,address,uint256)",
        ("address", "address", "uint256"), (customer, relayer, 7),
        contract=tokens["token721"]["address"], wait=False,
    )
    return tx20, tx721


def expected_transfer_values(descriptor):
    accounts = descriptor["publicFixtureAccounts"]
    admin = to_checksum_address(accounts["admin"]["address"])
    customer = to_checksum_address(accounts["customer"]["address"])
    relayer = to_checksum_address(accounts["relayer"]["address"])
    return {
        "token20": ((to_checksum_address(ZERO), admin, 1000),
                    (admin, customer, 100), (admin, relayer, 25)),
        "token721": ((to_checksum_address(ZERO), admin, 7),
                     (admin, customer, 7), (customer, relayer, 7)),
    }


async def verify_token_history(reader, checkpoint, descriptor, tokens, receipts, rpc):
    """Prove both ABIs coexist under one checkpoint using actual receipt bytes."""
    admin = descriptor["publicFixtureAccounts"]["admin"]["address"]
    customer = descriptor["publicFixtureAccounts"]["customer"]["address"]
    relayer = descriptor["publicFixtureAccounts"]["relayer"]["address"]
    expected = expected_transfer_values(descriptor)
    for alias in ("token20", "token721"):
        address = tokens[alias]["address"]
        constructor = await wait_for_receipt(rpc, descriptor, tokens[alias]["creationTransaction"])
        actual_receipts = (constructor, receipts["initial"][alias], receipts["mixed"][alias])
        page = await reader.events(
            checkpoint=checkpoint, contract=alias, event=TRANSFER, where={}, limit=10,
        )
        require(page.complete and len(page.events) == 3,
                "Shared checkpoint has incomplete real token Transfer history.")
        for (item, values, receipt) in zip(page.events, expected[alias], actual_receipts, strict=True):
            require(item.values == values,
                    "Generic reader decoded a real ERC token Transfer incorrectly.")
            assert_event_matches_receipt(item, only_transfer(receipt, address))
            require(
                (len(item.topics), len(item.data))
                == ((3, 32) if alias == "token20" else (4, 0)),
                "Identical ERC Transfer signatures lost their distinct indexed layouts.",
            )

    require(
        only_transfer(receipts["mixed"]["token20"], tokens["token20"]["address"])["topics"][0].lower()
        == only_transfer(receipts["mixed"]["token721"], tokens["token721"]["address"])["topics"][0].lower(),
        "ERC-20 and ERC-721 fixtures must expose an identical Transfer signature.",
    )
    expected_block = receipts["mixed"]["token20"]["blockHash"]
    require(
        receipts["mixed"]["token721"]["blockHash"] == expected_block
        and receipts["mixed"]["payment"]["blockHash"] == expected_block
        and checkpoint.block.hash.hex() == expected_block[2:].lower(),
        "One checkpoint did not publish the actual mixed-contract block.",
    )

    token20 = tokens["token20"]["address"]
    token721 = tokens["token721"]["address"]
    for (account, balance) in ((admin, 875), (customer, 100), (relayer, 25)):
        require(
            await call_token_view(rpc, descriptor, token20, "balanceOf(address)",
                       ("address",), (account,), ("uint256",)) == (balance,),
            "ERC-20 on-chain balances contradict the emitted test Transfers.",
        )
    require((await call_token_view(rpc, descriptor, token721, "ownerOf(uint256)",
                        ("uint256",), (7,), ("address",)))[0].lower() == relayer.lower(),
            "ERC-721 on-chain ownership contradicts emitted test Transfers.")


async def verify_after_restart(reader, checkpoint, tokens, descriptor):
    """A new process must observe exact preexisting selected event histories."""
    expected = expected_transfer_values(descriptor)
    for alias in ("token20", "token721"):
        page = await reader.events(
            checkpoint=checkpoint, contract=alias, event=TRANSFER, where={}, limit=10,
        )
        require(page.complete and tuple(event.values for event in page.events) == expected[alias]
                and all(event.address.lower() == tokens[alias]["address"].lower()
                        for event in page.events),
                "Restart changed the checkpoint-bound token history.")
