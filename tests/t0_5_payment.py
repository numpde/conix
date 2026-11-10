"""Real PayToPlay acceptance against a dapp32-owned Anvil and Conix PostgreSQL.

The chain mutations below are permitted ONLY for a campaign-owned disposable
fixture. They are test actions, not Conix indexing/signing implementations.
The test oracle is the deployed contract's receipts and known transaction values.
"""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

from eth_abi import decode
from eth_utils import keccak, to_checksum_address
from eth_utils.abi import abi_to_signature, collapse_if_tuple

from conix.catalog import prepare_catalog
from conix.core import BranchConflict, open_writer
from conix.journal import Block
from conix.reader import Checkpoint, CursorInvalid, open_reader
from abi_contract_vectors import VOUCHER_SIGNATURE as VOUCHER
from fixture_protocol import ScenarioFailure
from t0_5_chain import (
    assert_event_matches_receipt, calldata, require, send, wait_for_receipt,
)
from t0_5_tokens import (
    emit_initial_transfers, only_transfer, submit_pending_token_transfers,
    verify_after_restart, verify_token_history,
)

COLLECT = "collect(address,uint256,uint256,bytes32,uint256,bytes)"
TRANSFER_BYTES = re.compile(r"0x[0-9a-fA-F]{64}\Z")
SIGNATURE_BYTES = re.compile(r"0x[0-9a-fA-F]{130}\Z")


def deployed_payment_function(descriptor, name):
    """Locate the named function in the actual Forge deployment ABI."""
    declarations = [
        entry for entry in descriptor["contracts"]["PayToPlay"]["abi"]
        if entry.get("type") == "function" and entry.get("name") == name
    ]
    require(len(declarations) == 1, "Deployed PayToPlay ABI lacks a unique requested function.")
    return declarations[0]


def named_position(descriptor, values):
    """Attach Solidity field names to the independent decoded position tuple."""
    function = deployed_payment_function(descriptor, "viewPosition")
    components = function["outputs"][0]["components"]
    return {
        component["name"]: value
        for (component, value) in zip(components, values, strict=True)
    }


async def payment_call(rpc, descriptor, name, arguments):
    """Use the deployed PayToPlay ABI as the view-call type authority.

    This is direct JSON-RPC, independent of Conix's caller encoder and
    return decoder. The exact Solidity declaration supplies the tuple shape.
    """
    function = deployed_payment_function(descriptor, name)
    signature = abi_to_signature(function)
    input_types = tuple(collapse_if_tuple(arg) for arg in function["inputs"])
    output_types = tuple(collapse_if_tuple(arg) for arg in function["outputs"])
    transaction = {
        "to": descriptor["contracts"]["PayToPlay"]["address"],
        "data": calldata(signature, input_types, arguments),
    }

    result = await rpc(descriptor, "eth_call", [transaction, "latest"])

    require(type(result) is str and result.startswith("0x"), "PayToPlay view has no ABI result.")
    return decode(output_types, bytes.fromhex(result[2:]))


async def fixture_voucher_signer_address(rpc, descriptor):
    """Use a pre-funded, unlocked Anvil account; handle no wallet keys."""
    addresses = await rpc(descriptor, "eth_accounts", [])
    excluded = {
        entry["address"].lower()
        for entry in descriptor["publicFixtureAccounts"].values()
    }
    require(type(addresses) is list, "Anvil did not expose its test signer accounts.")
    for address in addresses:
        if type(address) is str and address.lower() not in excluded:
            return address
    raise ScenarioFailure("Anvil has no independent unlocked fixture voucher signer.")


async def sign_voucher(rpc, descriptor, *, signer, customer, channel, total, invoice):
    """Ask Anvil's unlocked test wallet to sign; verify the deployed digest."""
    (digest,) = await payment_call(
        rpc, descriptor, "voucherDigest",
        (customer, channel, total, invoice),
    )
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Voucher": [
                {"name": "customer", "type": "address"},
                {"name": "channelId", "type": "uint256"},
                {"name": "cumulativeTotal", "type": "uint256"},
                {"name": "invoiceHash", "type": "bytes32"},
            ],
        },
        "primaryType": "Voucher",
        "domain": {
            "name": "PayToPlay",
            "version": "2",
            "chainId": descriptor["chainId"],
            "verifyingContract": descriptor["contracts"]["PayToPlay"]["address"],
        },
        "message": {
            "customer": customer,
            "channelId": str(channel),
            "cumulativeTotal": str(total),
            "invoiceHash": "0x" + invoice.hex(),
        },
    }
    signature = await rpc(descriptor, "eth_signTypedData_v4", [signer, typed])
    require(type(signature) is str and SIGNATURE_BYTES.fullmatch(signature) is not None,
            "Anvil returned an invalid signed voucher.")

    # Cast independently recovers the signer from the deployed digest.
    # This does not reproduce the contract's EIP-712 hashing implementation.
    process = await asyncio.create_subprocess_exec(
        "cast", "wallet", "verify", "--no-hash",
        "--address", signer, "0x" + digest.hex(), signature,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        status = await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError as error:
        raise ScenarioFailure(
            "Cast voucher verification timed out; no collection transaction was submitted."
        ) from error
    finally:
        # A cancelled test must not orphan Cast, and an already-exited child
        # must not replace the original failure with ProcessLookupError.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    require(status == 0, "Fixture signature differs from the deployed voucher digest.")
    return bytes.fromhex(signature[2:])

async def scan_to_tip(writer):
    """Complete the selected prefix, refusing an unexpectedly long fixture.

    The campaign is a bounded acceptance test, not an unattended catch-up
    worker. A continuously changing chain must not keep it running forever.
    """
    for _ in range(256):
        if await writer.advance_one() is None:
            return

    raise ScenarioFailure("T0-5 fixture exceeded 256 blocks without reaching the tip.")


def selected_receipt_log(receipt, contract):
    topic0 = "0x" + keccak(text=VOUCHER).hex()
    selected = [
        log for log in receipt["logs"]
        if log["address"].lower() == contract.lower()
        and log["topics"][0].lower() == topic0.lower()
    ]
    require(len(selected) == 1, "Mined positive collection must have exactly one collection event.")
    return selected[0]


def checkpoint_record(checkpoint):
    return {
        "source": checkpoint.source_id.hex(),
        "generation": str(checkpoint.generation),
        "start": checkpoint.history_start,
        "revision": checkpoint.revision,
        "block": {
            "number": checkpoint.block.number,
            "hash": checkpoint.block.hash.hex(),
            "parent": checkpoint.block.parent_hash.hex(),
            "timestamp": checkpoint.block.timestamp,
        },
    }


def saved_checkpoint(record):
    """Reconstruct a checkpoint after shared public-receipt admission."""
    block = record["block"]
    return Checkpoint(
        source_id=bytes.fromhex(record["source"]),
        generation=UUID(record["generation"]),
        history_start=record["start"],
        block=Block(
            number=block["number"],
            hash=bytes.fromhex(block["hash"]),
            parent_hash=bytes.fromhex(block["parent"]),
            timestamp=block["timestamp"],
        ),
        revision=record["revision"],
    )

async def read_position(reader, descriptor, checkpoint, customer, channel):
    """Read the position at a pinned checkpoint, preserving the ABI's names."""
    (values,) = await reader.call(
        checkpoint=checkpoint,
        contract="payment",
        function="viewPosition(address,uint256,address)",
        arguments=(customer, channel, customer),
    )
    return named_position(descriptor, values)


async def collect_voucher(rpc, descriptor, *, signer, customer, collector, channel, total, cap, invoice, wait=True):
    signature = await sign_voucher(
        rpc, descriptor, signer=signer, customer=customer, channel=channel, total=total, invoice=invoice,
    )
    return await send(
        rpc, descriptor, collector, COLLECT,
        ("address", "uint256", "uint256", "bytes32", "uint256", "bytes"),
        (customer, channel, total, invoice, cap, signature), wait=wait,
    )


async def prepare_positions(rpc, descriptor, *, voucher_signer_address):
    """Create two service channels and three funded positions on fresh Anvil.

    The admin also acts as the second customer; the relayer alone collects.
    The separate voucher key prevents conflating those three authorities.
    """
    accounts = descriptor["publicFixtureAccounts"]
    admin = accounts["admin"]["address"]
    customer = accounts["customer"]["address"]
    second_customer = admin
    collector = accounts["relayer"]["address"]
    payee = accounts["payee"]["address"]
    (count,) = await payment_call(rpc, descriptor, "channelCount", ())
    require(count == 1, "Fresh PayToPlay fixture unexpectedly has service channels.")
    for _ in range(2):
        await send(
            rpc, descriptor, admin, "createChannel(address,address,uint64)",
            ("address", "address", "uint64"), (payee, collector, 60),
        )
    for (actor, channel) in ((customer, 1), (customer, 2), (second_customer, 1)):
        await send(
            rpc, descriptor, actor, "registerVoucherKey(uint256,address)",
            ("uint256", "address"), (channel, voucher_signer_address),
        )
    for (actor, channel, amount) in ((customer, 1, 100), (customer, 2, 20),
                                    (second_customer, 1, 25)):
        await send(
            rpc, descriptor, actor, "fund(uint256,uint256)",
            ("uint256", "uint256"), (channel, amount), value=amount,
        )


async def mine_mixed_block(rpc, descriptor, tokens, *, signer, customer, collector, invoice):
    """Publish three different selected contracts in the SAME mined block.

    The automining setting is restored even on a rejected submission. This
    function may be called only for the campaign-owned disposable Anvil.
    """
    mining_error = None

    try:
        # Even a lost start reply can leave automining disabled.
        await rpc(descriptor, "anvil_setAutomine", [False])
        payment_tx = await collect_voucher(
            rpc, descriptor, signer=signer, customer=customer, collector=collector,
            channel=1, total=70, cap=30, invoice=invoice, wait=False,
        )
        (token20_tx, token721_tx) = await submit_pending_token_transfers(rpc, descriptor, tokens)
        await rpc(descriptor, "evm_mine", [])
    except BaseException as error:
        mining_error = error
        raise
    finally:
        try:
            await rpc(descriptor, "anvil_setAutomine", [True])
        except BaseException as restoration_error:
            if mining_error is None:
                raise
            mining_error.add_note(
                f"Anvil automining restoration failed ({type(restoration_error).__name__}); "
                "the fixture state requires inspection."
            )

    return {
        "payment": await wait_for_receipt(rpc, descriptor, payment_tx),
        "token20": await wait_for_receipt(rpc, descriptor, token20_tx),
        "token721": await wait_for_receipt(rpc, descriptor, token721_tx),
    }


async def assert_collection_history(reader, checkpoint, *, expected, customer, payment_address, payee):
    """A SQL-filtered customer/channel view must match independent mined logs."""
    by_position = {}
    for (account, channel, mined_receipt, values) in expected:
        by_position.setdefault((account, channel), []).append((mined_receipt, values))

    for ((account, channel), observations) in by_position.items():
        page = await reader.events(
            checkpoint=checkpoint,
            contract="payment",
            event=VOUCHER,
            where={"customer": account, "channelId": channel},
            limit=100,
        )
        require(
            page.complete and len(page.events) == len(observations),
            "The filtered checkpoint has missing or unexpected payment events.",
        )

        for (event, (mined_receipt, outcome)) in zip(
            page.events, observations, strict=True,
        ):
            assert_event_matches_receipt(
                event, selected_receipt_log(mined_receipt, payment_address),
            )
            require(
                event.values == (
                    to_checksum_address(account),
                    channel,
                    to_checksum_address(payee),
                    *outcome,
                ),
                "The indexed event has incorrect typed payment values.",
            )

    first_page = await reader.events(
        checkpoint=checkpoint, contract="payment", event=VOUCHER,
        where={"customer": customer, "channelId": 1}, limit=1,
    )
    require(len(first_page.events) == 1 and not first_page.complete
            and first_page.next_cursor is not None,
            "First filtered page is not a proper continuation.")
    last_page = await reader.events(
        checkpoint=checkpoint, contract="payment", event=VOUCHER,
        where={"channelId": 1, "customer": customer},
        cursor=first_page.next_cursor, limit=1,
    )
    require(len(last_page.events) == 1 and last_page.complete
            and last_page.next_cursor is None,
            "Final filtered page does not close the exact checkpoint.")
    require([e.values[3] for e in (*first_page.events, *last_page.events)] == [30, 40],
            "Customer/channel filtered pagination omitted or duplicated a collection.")

    empty = await reader.events(
        checkpoint=checkpoint, contract="payment", event=VOUCHER,
        where={"customer": customer, "channelId": 999}, limit=1,
    )
    require(empty.complete and empty.events == () and empty.next_cursor is None,
            "Unmatched selected history is not an empty complete page.")
    try:
        await reader.events(
            checkpoint=checkpoint, contract="payment", event=VOUCHER,
            where={"customer": customer, "channelId": 2},
            cursor=first_page.next_cursor, limit=1,
        )
    except CursorInvalid:
        pass
    else:
        raise ScenarioFailure("Cursor was accepted with a different indexed filter.")


async def exercise(descriptor, catalog_file, rpc, *, writer_dsn, reader_dsn, tokens):
    """Prove signed settlement, actual indexed history and historical state."""
    catalog = prepare_catalog(catalog_file)
    accounts = descriptor["publicFixtureAccounts"]
    admin = accounts["admin"]["address"]
    customer = accounts["customer"]["address"]
    second_customer = admin
    collector = accounts["relayer"]["address"]
    payee = accounts["payee"]["address"]
    voucher_signer = await fixture_voucher_signer_address(rpc, descriptor)
    payment = descriptor["contracts"]["PayToPlay"]["address"]
    invoice_a = bytes.fromhex("11" * 32)
    invoice_b = bytes.fromhex("22" * 32)
    invoice_c = bytes.fromhex("33" * 32)
    before_payee = int(await rpc(descriptor, "eth_getBalance", [payee, "latest"]), 16)

    await prepare_positions(rpc, descriptor, voucher_signer_address=voucher_signer)

    # Token constructor Transfer events already exist from preparation.
    # These transactions add distinct real emitted values before the mixed block.
    initial_tokens = await emit_initial_transfers(rpc, descriptor, tokens)

    async with open_writer(
        catalog=catalog,
        database_dsn=writer_dsn,
        rpc_url=descriptor["rpcUrl"],
    ) as writer:
        await scan_to_tip(writer)
        async with open_reader(
            catalog=catalog,
            database_dsn=reader_dsn,
            rpc_url=descriptor["rpcUrl"],
        ) as reader:
            funded = await reader.checkpoint()
            position_state = await read_position(
                reader, descriptor, funded, customer, 1,
            )
            require(
                (position_state["balance"], position_state["voucherPaid"]) == (100, 0),
                "Funding checkpoint has the wrong position state.",
            )

        mixed_tokens = await mine_mixed_block(
            rpc, descriptor, tokens,
            signer=voucher_signer, customer=customer, collector=collector, invoice=invoice_a,
        )
        first = mixed_tokens["payment"]
        await scan_to_tip(writer)
        async with open_reader(
            catalog=catalog,
            database_dsn=reader_dsn,
            rpc_url=descriptor["rpcUrl"],
        ) as reader:
            after_first = await reader.checkpoint()
            position_state = await read_position(
                reader, descriptor, after_first, customer, 1,
            )
            require(
                (position_state["balance"], position_state["voucherPaid"]) == (70, 30),
                "First collection checkpoint has the wrong position state.",
            )
            await verify_token_history(
                reader, after_first, descriptor, tokens,
                {"initial": initial_tokens, "mixed": mixed_tokens}, rpc,
            )

        second = await collect_voucher(
            rpc, descriptor, signer=voucher_signer, customer=customer, collector=collector,
            channel=1, total=70, cap=70, invoice=invoice_a,
        )
        await scan_to_tip(writer)
        async with open_reader(
            catalog=catalog,
            database_dsn=reader_dsn,
            rpc_url=descriptor["rpcUrl"],
        ) as reader:
            after_second = await reader.checkpoint()
            position_state = await read_position(
                reader, descriptor, after_second, customer, 1,
            )
            require(
                (position_state["balance"], position_state["voucherPaid"]) == (30, 70),
                "Second collection checkpoint has the wrong position state.",
            )

        zero = await collect_voucher(
            rpc, descriptor, signer=voucher_signer, customer=customer, collector=collector,
            channel=1, total=70, cap=70, invoice=invoice_a,
        )
        require(not any(
            log["topics"][0].lower() == ("0x" + keccak(text=VOUCHER).hex())
            and log["address"].lower() == payment.lower()
            for log in zero["logs"]
        ), "Exhausted voucher unexpectedly emitted a collection event.")

        other_channel = await collect_voucher(
            rpc, descriptor, signer=voucher_signer, customer=customer, collector=collector,
            channel=2, total=15, cap=15, invoice=invoice_b,
        )
        other_actor = await collect_voucher(
            rpc, descriptor, signer=voucher_signer, customer=second_customer, collector=collector,
            channel=1, total=20, cap=20, invoice=invoice_c,
        )

        await send(
            rpc, descriptor, customer, "requestWithdrawal(uint256,uint256)",
            ("uint256", "uint256"), (1, 30),
        )
        ready = await payment_call(
            rpc, descriptor, "viewPosition", (customer, 1, customer),
        )
        ready_at = named_position(descriptor, ready[0])["readyAt"]
        tip = await rpc(descriptor, "eth_getBlockByNumber", ["latest", False])
        tip_timestamp = int(tip["timestamp"], 16)
        require(ready_at > tip_timestamp, "Withdrawal readiness did not advance after request.")
        await rpc(descriptor, "evm_increaseTime", [ready_at - tip_timestamp])
        await rpc(descriptor, "evm_mine", [])
        withdrawal = await send(
            rpc, descriptor, collector, "withdraw(address,uint256)",
            ("address", "uint256"), (customer, 1),
        )
        require(withdrawal["status"] == "0x1", "Due withdrawal did not complete.")
        await scan_to_tip(writer)

        async with open_reader(
            catalog=catalog,
            database_dsn=reader_dsn,
            rpc_url=descriptor["rpcUrl"],
        ) as reader:
            current = await reader.checkpoint()
            position_state = await read_position(
                reader, descriptor, current, customer, 1,
            )
            require(
                (position_state["balance"], position_state["voucherPaid"]) == (0, 70),
                "Withdrawal checkpoint has the wrong position state.",
            )
            position_state = await read_position(
                reader, descriptor, funded, customer, 1,
            )
            require(
                (position_state["balance"], position_state["voucherPaid"]) == (100, 0),
                "Later appends changed the funding historical view.",
            )
            position_state = await read_position(
                reader, descriptor, after_first, customer, 1,
            )
            require(
                (position_state["balance"], position_state["voucherPaid"]) == (70, 30),
                "Later appends changed the first historical view.",
            )
            position_state = await read_position(
                reader, descriptor, after_second, customer, 1,
            )
            require(
                (position_state["balance"], position_state["voucherPaid"]) == (30, 70),
                "Later appends changed the second historical view.",
            )

            expected = [
                (customer, 1, first, (30, 30, invoice_a)),
                (customer, 1, second, (40, 70, invoice_a)),
                (customer, 2, other_channel, (15, 15, invoice_b)),
                (second_customer, 1, other_actor, (20, 20, invoice_c)),
            ]
            await assert_collection_history(
                reader, current, expected=expected,
                customer=customer, payment_address=payment, payee=payee,
            )
            payee_after = int(await rpc(descriptor, "eth_getBalance", [payee, "latest"]), 16)
            require(payee_after - before_payee == 105,
                    "Actual payee wallet did not receive the collected native asset.")
            return {
                "schema": "conix.acceptance.payments.v1",
                "checkpoint": checkpoint_record(current),
                "transactions": [receipt["transactionHash"] for (_, _, receipt, _) in expected],
                "collected": [30, 40],
            }


def admit_exercise(state):
    """The operator and this fresh process use one public receipt contract."""
    from fixture_protocol import admit_payments

    return saved_checkpoint(admit_payments(state)["checkpoint"])

async def resume(descriptor, catalog_file, rpc, *, writer_dsn, reader_dsn, state, tokens):
    """Run in a NEW container/process after the producing phase has exited."""
    before = admit_exercise(state)
    catalog = prepare_catalog(catalog_file)
    require(before.source_id == catalog.source_id,
            "Restarted process compiled a different selected event source.")

    async with open_writer(
        catalog=catalog,
        database_dsn=writer_dsn,
        rpc_url=descriptor["rpcUrl"],
    ) as writer:
        require(await writer.advance_one() is None,
                "A fresh writer republished an already indexed block.")
        await rpc(descriptor, "evm_mine", [])
        continued = await writer.advance_one()
        require(continued is not None and continued.last_block.number == before.block.number + 1,
                "Fresh writer did not continue from the persisted checkpoint.")

    async with open_reader(
        catalog=catalog,
        database_dsn=reader_dsn,
        rpc_url=descriptor["rpcUrl"],
    ) as reader:
        newest = await reader.checkpoint()
        require(newest.generation == before.generation,
                "Ordinary append changed the retained history generation.")
        page = await reader.events(
            checkpoint=before, contract="payment", event=VOUCHER,
            where={
                "customer": descriptor["publicFixtureAccounts"]["customer"]["address"],
                "channelId": 1,
            },
            limit=10,
        )
        require(page.complete and [e.values[3] for e in page.events] == state["collected"],
                "Restarted reader changed previously verified filtered history.")
        all_events = await reader.events(
            checkpoint=newest, contract="payment", event=VOUCHER,
            where={}, limit=10,
        )
        require(all_events.complete and len(all_events.events) == 4
                and {e.transaction_hash.hex() for e in all_events.events}
                == {tx[2:].lower() for tx in state["transactions"]},
                "Restarted reader lost, duplicated or added mined positive collections.")
        await verify_after_restart(reader, before, tokens, descriptor)
        await verify_after_restart(reader, newest, tokens, descriptor)


async def reorganize(descriptor, catalog_file, rpc, *, writer_dsn, reader_dsn, state, tokens):
    """Replace actual Anvil branch A with B; only the owned campaign may call."""
    previous = admit_exercise(state)
    catalog = prepare_catalog(catalog_file)
    account = descriptor["publicFixtureAccounts"]["customer"]["address"]
    collector = descriptor["publicFixtureAccounts"]["relayer"]["address"]
    voucher_signer = await fixture_voucher_signer_address(rpc, descriptor)
    invoice = bytes.fromhex("44" * 32)

    async with open_writer(
        catalog=catalog,
        database_dsn=writer_dsn,
        rpc_url=descriptor["rpcUrl"],
    ) as writer:
        ancestor = (await writer.inspect()).last_block
        require(ancestor is not None and ancestor.number > previous.block.number,
                "Reorganization was not started from the restarted durable prefix.")
        snapshot = await rpc(descriptor, "evm_snapshot", [])
        branch_a = await collect_voucher(
            rpc, descriptor, signer=voucher_signer, customer=account, collector=collector,
            channel=2, total=20, cap=5, invoice=invoice,
        )
        token_a = await send(
            rpc, descriptor, descriptor["publicFixtureAccounts"]["admin"]["address"],
            "transfer(address,uint256)", ("address", "uint256"), (account, 5),
            contract=tokens["token20"]["address"],
        )
        await scan_to_tip(writer)

        async with open_reader(
            catalog=catalog,
            database_dsn=reader_dsn,
            rpc_url=descriptor["rpcUrl"],
        ) as reader:
            old = await reader.checkpoint()
            page = await reader.events(
                checkpoint=old, contract="payment", event=VOUCHER,
                where={"customer": account, "channelId": 2}, limit=1,
            )
            require(not page.complete and page.next_cursor is not None,
                    "Branch A did not yield a real stale pagination cursor.")
            old_cursor = page.next_cursor

        require(await rpc(descriptor, "evm_revert", [snapshot]) is True,
                "Owned Anvil fixture did not revert to the shared ancestor.")
        # Canonical branch B changes a different selected contract.
        token_b = await send(
            rpc, descriptor, collector, "transferFrom(address,address,uint256)",
            ("address", "address", "uint256"),
            (collector, descriptor["publicFixtureAccounts"]["payee"]["address"], 7),
            contract=tokens["token721"]["address"],
        )
        await rpc(descriptor, "evm_mine", [])
        replacement = await rpc(descriptor, "eth_getBlockByNumber", [hex(old.block.number), False])
        require(replacement is not None
                and replacement["hash"].lower() != ("0x" + old.block.hash.hex()),
                "Reorganization fixture did not produce a different canonical block.")

        try:
            await writer.advance_one()
        except BranchConflict:
            pass
        else:
            raise ScenarioFailure("Writer silently advanced on conflicting canonical history.")
        require((await writer.inspect()).last_block == old.block,
                "Conflicting branch changed retained history before explicit repair.")

        repaired = await writer.repair_to(ancestor=ancestor)
        require(repaired.generation != old.generation
                and repaired.last_block == ancestor,
                "Explicit manual repair did not retire the conflicting suffix.")
        await scan_to_tip(writer)
        require((await writer.inspect()).last_block.hash.hex() == replacement["hash"][2:].lower(),
                "Reindexing did not publish the replacement canonical branch.")

        async with open_reader(
            catalog=catalog,
            database_dsn=reader_dsn,
            rpc_url=descriptor["rpcUrl"],
        ) as reader:
            current = await reader.checkpoint()
            try:
                await reader.events(
                    checkpoint=old, contract="payment", event=VOUCHER,
                    where={"customer": account, "channelId": 2},
                    cursor=old_cursor, limit=1,
                )
            except CursorInvalid:
                pass
            else:
                raise ScenarioFailure("A retired branch cursor remained usable after repair.")
            page = await reader.events(
                checkpoint=current, contract="payment", event=VOUCHER,
                where={"customer": account, "channelId": 2},
                limit=10,
            )
            require(page.complete and [event.values[3] for event in page.events] == [15],
                    "Recovered history still includes a retired branch payment.")
            require(all(event.transaction_hash.hex() != branch_a["transactionHash"][2:].lower()
                        for event in page.events),
                    "Reorganized-out transaction survived Conix suffix repair.")
            for (alias, expected_count) in (("token20", 3), ("token721", 4)):
                events = await reader.events(
                    checkpoint=current, contract=alias,
                    event="Transfer(address,address,uint256)", where={}, limit=10,
                )
                require(events.complete and len(events.events) == expected_count,
                        "Multi-contract recovery retained the wrong token event history.")
                require(all(event.transaction_hash.hex()
                            != token_a["transactionHash"][2:].lower()
                            for event in events.events),
                        "A branch-A token transfer survived checked suffix repair.")
                if alias == "token721":
                    surviving = [
                        event for event in events.events
                        if event.transaction_hash.hex() == token_b["transactionHash"][2:].lower()
                    ]
                    require(len(surviving) == 1,
                            "Canonical branch-B token event was not reindexed.")
                    assert_event_matches_receipt(
                        surviving[0], only_transfer(token_b, tokens["token721"]["address"]),
                    )
