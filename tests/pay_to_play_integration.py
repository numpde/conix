"""Real PayToPlay integration checks using the installed, unchanged Tier-0 Conix.

This is a disposable *test-only* client of dapp32's verified fixture JSON.
Transactions use Anvil-only account impersonation and test-only Cast signing.
No signing code, keys, synthetic events or alternate indexer enter production.
The test runner reads the descriptor on stdin and emits only public phase facts.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_protocol import (
    FixtureError, ScenarioFailure, _bad_constant, _pairs, decode,
    phase_failure_record, receipt,
)

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from conix.chain import Chain
from conix.configuration import Settings, open_chain
from conix.engine import run_session
from conix.errors import BranchConflict
from conix.journal import Journal
from conix.models import Deployment, HistoryUnavailable, ObservationUnavailable
from conix.observer import Observer
from conix.rpc import RpcError, RpcProtocolError, RpcUnavailable, data, quantity
from conix.schema import initialize

ONE_MIB = 1024 * 1024
GAS = "0x1e8480"


def _input():
    source = sys.stdin.buffer.read(ONE_MIB + 1)
    if len(source) > ONE_MIB:
        raise FixtureError("Contained fixture request exceeded 1 MiB.")
    try:
        content = json.loads(
            source.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=_bad_constant,
        )
    except (ValueError, UnicodeError) as error:
        raise FixtureError("Contained fixture request is invalid JSON.") from None
    if type(content) is not dict or set(content) != {"descriptor", "state"}:
        raise FixtureError("Contained fixture request must include descriptor and prior phase state.")
    description = content["descriptor"]
    if type(description) is not dict:
        raise FixtureError("Contained fixture descriptor is missing.")
    fixture_id = os.environ.get("CONIX_FIXTURE_ID", "")
    descriptor = decode(
        (json.dumps(description, separators=(",", ":")) + "\n").encode(),
        fixture_id=fixture_id,
    )
    previous = content["state"]
    if previous is not None:
        previous = receipt((json.dumps(previous, separators=(",", ":")) + "\n").encode())
    return descriptor, previous


def _settings(descriptor):
    contract = descriptor["contracts"]["PayToPlay"]
    return Settings(
        deployment=Deployment(descriptor["chainId"], to_checksum_address(contract["address"])),
        start_block=contract["deploymentBlock"],
        rpc_url=descriptor["rpcUrl"],
        database_dsn_file=Path("/run/conix/connection.dsn"),
        runtime_role="conix_runtime",
        poll_seconds=0.05,
        rpc_timeout_seconds=5,
        max_response_bytes=1024 * 1024,
        max_connections=4,
        max_page_size=16,
        max_evidence_blocks=256,
        operation_timeout_seconds=30,
    )


def _summary(phase, checkpoint, snapshot):
    value = {
        "phase": phase,
        "generation": str(checkpoint.generation),
        "height": checkpoint.block.number,
        "block_hash": "0x" + checkpoint.block.hash.hex(),
        "collection_count": checkpoint.collection_count,
        "snapshot": snapshot,
    }
    receipt((json.dumps(value) + "\n").encode())
    return value


def _confirm(checkpoint, prior):
    if (
        str(checkpoint.generation) != prior["generation"]
        or checkpoint.block.number != prior["height"]
        or "0x" + checkpoint.block.hash.hex() != prior["block_hash"]
        or checkpoint.collection_count != prior["collection_count"]
    ):
        raise ScenarioFailure("Durable checkpoint differs from prior process's source generation or count.")


async def _follow(journal, chain, height):
    """Wait for the genuine Tier-0 run_session, never scan/publish ourselves."""
    stop = asyncio.Event()
    task = asyncio.create_task(run_session(journal, chain, stop))
    try:
        async with asyncio.timeout(80):
            while True:
                if task.done():
                    await task
                    raise ScenarioFailure("Tier-0 session ended without producing requested complete coverage.")
                state = await journal.basis()
                if state.checkpoint is not None and state.checkpoint.number >= height:
                    break
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except BaseException:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise


def _calldata(signature, types=(), arguments=()):
    return "0x" + (keccak(text=signature)[:4] + encode(types, arguments)).hex()


async def _call_uint(chain, signature, types=(), arguments=()):
    value = await chain.rpc.call("eth_call", [
        {"to": chain.settings.deployment.address,
         "data": _calldata(signature, types, arguments)}, "latest",
    ])
    return int.from_bytes(data(value, "contract view", size=32), "big")


def _contract_errors(descriptor):
    """Source-controlled names from the verified deployed contract ABI."""
    mapping = {}
    for item in descriptor["contracts"]["PayToPlay"]["abi"]:
        if type(item) is not dict or item.get("type") != "error":
            continue
        name, inputs = item.get("name"), item.get("inputs")
        if (type(name) is not str or not name.isascii() or not name.isidentifier()
                or len(name) > 64 or type(inputs) is not list
                or any(type(entry) is not dict or type(entry.get("type")) is not str
                       for entry in inputs)):
            continue
        signature = name + "(" + ",".join(entry["type"] for entry in inputs) + ")"
        mapping[keccak(text=signature)[:4].hex()] = name
    return mapping


async def _revert_reason(chain, tx, errors):
    """Optional read-only Anvil diagnostic; never echo an untrusted trace."""
    try:
        trace = await chain.rpc.call("debug_traceTransaction", [
            tx, {"tracer": "callTracer", "tracerConfig": {"onlyTopCall": True},
                 "timeout": "5s"},
        ])
    except Exception:
        return "trace unavailable"
    if type(trace) is not dict:
        return "trace unavailable"
    output = trace.get("output")
    if type(output) is str and output.startswith("0x") and len(output) >= 10:
        selector = output[2:10].lower()
        if selector in errors:
            return "Solidity error " + errors[selector]
        if all(char in "0123456789abcdef" for char in selector):
            return "unknown Solidity error selector 0x" + selector
    if trace.get("error") == "out of gas":
        return "out of gas"
    return "revert reason unavailable"


async def _send(chain, sender, signature, types=(), arguments=(), *, value=0, errors=None):
    """Anvil-only transaction; require an actual successful mined receipt."""
    rpc = chain.rpc
    await rpc.call("anvil_impersonateAccount", [sender])
    try:
        transaction = {
            "from": sender,
            "to": chain.settings.deployment.address,
            "data": _calldata(signature, types, arguments),
            "gas": GAS,
        }
        if value:
            transaction["value"] = hex(value)
        action = signature.split("(", 1)[0]
        try:
            tx_hash = data(await rpc.call("eth_sendTransaction", [transaction]),
                           "transaction hash", size=32)
        except RpcError as error:
            raise ScenarioFailure(
                f"Fixture {action} submission returned RPC code {error.code}; "
                "outcome unconfirmed; do not resubmit."
            ) from error
        except (RpcProtocolError, RpcUnavailable, TimeoutError) as error:
            raise ScenarioFailure(
                f"Fixture {action} submission failed ({type(error).__name__}); "
                "outcome unconfirmed; do not resubmit."
            ) from error
        # Submission can succeed before the transaction is mined. Poll
        # receipt *reads* briefly; never rebroadcast an accepted transaction.
        mined = None
        try:
            for attempt in range(10):
                mined = await rpc.call("eth_getTransactionReceipt", ["0x" + tx_hash.hex()])
                if mined is not None:
                    break
                if attempt != 9:
                    await asyncio.sleep(0.2)
        except (RpcError, RpcProtocolError, RpcUnavailable, TimeoutError) as error:
            raise ScenarioFailure(
                f"Fixture {action} receipt lookup failed ({type(error).__name__}); "
                "transaction outcome unconfirmed; do not resubmit."
            ) from error
        if mined is None:
            raise ScenarioFailure(
                f"Fixture {action} has no mined receipt after submission; "
                "outcome unknown; do not resubmit."
            )
        if type(mined) is not dict:
            raise ScenarioFailure(
                f"Fixture {action} receipt has an invalid shape; "
                "outcome unknown; do not resubmit."
            )
        receipt_hash = mined.get("transactionHash")
        if type(receipt_hash) is not str or receipt_hash.lower() != ("0x" + tx_hash.hex()):
            raise ScenarioFailure(
                f"Fixture {action} receipt names a different transaction; "
                "outcome unknown; do not resubmit."
            )
        if mined.get("status") != "0x1":
            used = mined.get("gasUsed")
            try:
                gas = quantity(used, "receipt gas used")
            except RpcProtocolError:
                gas = None
            status = mined.get("status")
            outcome = "reverted" if status == "0x0" else "unknown receipt status"
            gas_detail = f"; gas used {gas} of {int(GAS, 16)}" if gas is not None else ""
            reason = await _revert_reason(chain, "0x" + tx_hash.hex(), errors or {}) if status == "0x0" else ""
            detail = f"; {reason}" if reason else ""
            raise ScenarioFailure(
                f"Fixture {action} {outcome}{gas_detail}{detail}; do not resubmit."
            )
        return mined
    finally:
        prior = sys.exc_info()[1]
        try:
            await rpc.call("anvil_stopImpersonatingAccount", [sender])
        except Exception as cleanup:
            action = signature.split("(", 1)[0]
            if type(prior) is ScenarioFailure:
                raise ScenarioFailure(
                    f"{prior} Anvil impersonation cleanup also failed "
                    f"({type(cleanup).__name__})."
                ) from prior
            if prior is not None:
                raise ScenarioFailure(
                    f"Fixture {action} failed ({type(prior).__name__}); "
                    f"Anvil impersonation cleanup also failed ({type(cleanup).__name__})."
                ) from prior
            raise ScenarioFailure(
                f"Fixture {action} succeeded but Anvil impersonation cleanup failed "
                f"({type(cleanup).__name__}); fixture state requires inspection."
            ) from cleanup


async def _voucher_signature(chain, key, customer, channel, cumulative, invoice):
    # The real deployed contract, not a second Python EIP-712 implementation,
    # defines the domain and voucher digest.
    digest = data(await chain.rpc.call("eth_call", [
        {"to": chain.settings.deployment.address,
         "data": _calldata(
             "voucherDigest(address,uint256,uint256,bytes32)",
             ("address", "uint256", "uint256", "bytes32"),
             (customer, channel, cumulative, invoice),
         )}, "latest",
    ]), "voucher digest", size=32)
    try:
        signed = subprocess.run(
            ["cast", "wallet", "sign", "--no-hash", "0x" + digest.hex(),
             "--private-key", key],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScenarioFailure("Pinned test-only Cast signing operation failed.") from None
    signature = signed.stdout.strip()
    if signed.returncode != 0 or len(signature) != 132 or not signature.startswith("0x"):
        raise ScenarioFailure("Pinned test-only Cast returned no valid voucher signature.")
    result = bytes.fromhex(signature[2:])
    if len(result) != 65 or result[64] not in (27, 28):
        raise ScenarioFailure("Test-only voucher signature lacks a recoverable ECDSA identifier.")
    return result


async def _events(observer, checkpoint):
    observed = []
    cursor = None
    for _ in range(checkpoint.collection_count + 1):
        page = await observer.voucher_collected_events(checkpoint, cursor=cursor, limit=1)
        observed.extend(page.voucher_collected_events)
        cursor = page.cursor
        if page.complete:
            if len(observed) != checkpoint.collection_count:
                raise ScenarioFailure("Collection pages do not cover exactly the checkpoint count.")
            return tuple(observed)
    raise ScenarioFailure("Collection pagination failed to terminate within the known complete count.")


async def _baseline(journal, chain, observer, _descriptor, prior):
    if prior is not None:
        raise FixtureError("Baseline has no earlier fixture phase.")
    head = await chain.current_head()
    await _follow(journal, chain, head.number)
    checkpoint = await observer.latest_checkpoint()
    if checkpoint.block != head or checkpoint.collection_count != 0:
        raise ScenarioFailure("Fresh dapp32 base deployment should have complete, collection-free coverage.")
    snapshot = await chain.rpc.call("evm_snapshot", [])
    if type(snapshot) is not str or not 0 < len(snapshot) <= 80:
        raise ScenarioFailure("Anvil did not confirm a bounded snapshot identity.")
    return _summary("baseline", checkpoint, snapshot)


async def _collect(journal, chain, observer, descriptor, prior):
    if prior is None or prior["phase"] != "baseline":
        raise FixtureError("Collection requires a verified baseline receipt.")
    starting = await observer.latest_checkpoint()
    _confirm(starting, prior)
    accounts = descriptor["publicFixtureAccounts"]
    admin = to_checksum_address(accounts["admin"]["address"])
    customer = to_checksum_address(accounts["customer"]["address"])
    collector = to_checksum_address(accounts["relayer"]["address"])
    payee = to_checksum_address(accounts["payee"]["address"])
    channel = await _call_uint(chain, "channelCount()")
    errors = _contract_errors(descriptor)
    await _send(chain, admin, "createChannel(address,address,uint64)",
                ("address", "address", "uint64"), (payee, collector, 3600), errors=errors)
    await _send(chain, customer, "registerVoucherKey(uint256,address)",
                ("uint256", "address"), (channel, customer), errors=errors)
    await _send(chain, customer, "fund(uint256,uint256)",
                ("uint256", "uint256"), (channel, 100), value=100, errors=errors)
    invoices = (bytes.fromhex("61" * 32), bytes.fromhex("62" * 32))
    first_signature = await _voucher_signature(
        chain, accounts["customer"]["privateKey"], customer, channel, 40, invoices[0]
    )
    first = await _send(chain, collector,
        "collect(address,uint256,uint256,bytes32,uint256,bytes)",
        ("address", "uint256", "uint256", "bytes32", "uint256", "bytes"),
        (customer, channel, 40, invoices[0], 40, first_signature), errors=errors)
    await _send(chain, admin, "setPayee(uint256,address)",
                ("uint256", "address"), (channel, admin), errors=errors)
    second_signature = await _voucher_signature(
        chain, accounts["customer"]["privateKey"], customer, channel, 60, invoices[1]
    )
    second = await _send(chain, collector,
        "collect(address,uint256,uint256,bytes32,uint256,bytes)",
        ("address", "uint256", "uint256", "bytes32", "uint256", "bytes"),
        (customer, channel, 60, invoices[1], 20, second_signature), errors=errors)
    mined_head = await chain.current_head()
    await _follow(journal, chain, mined_head.number)
    checkpoint = await observer.latest_checkpoint()
    observed = await _events(observer, checkpoint)
    if len(observed) != 2:
        raise ScenarioFailure("Real signed collections did not produce exactly two indexed events.")
    for index, (item, raw, amount, paid, recipient) in enumerate(
        zip(observed, (first, second), (40, 20), (40, 60), (payee, admin))
    ):
        tx = data(raw["transactionHash"], "receipt transaction", size=32)
        if (item.transaction_hash != tx or item.customer != customer or item.channel_id != channel
            or item.amount != amount or item.voucher_paid != paid or item.payee != recipient
            or item.invoice_hash != invoices[index]):
            raise ScenarioFailure("Indexed collection differs from the real receipt/typed voucher/payee.")
    observation = await observer.latest_position(customer, channel)
    if (observation.checkpoint.block != checkpoint.block
        or observation.position.voucher_paid != 60
        or observation.position.balance != 40
        or observation.position.payee != admin
        or observation.position.voucher_key != customer):
        raise ScenarioFailure("Block-pinned position disagrees with collected contract state.")
    evidence = await observer.canonical_evidence(
        checkpoint, tuple(item.block_number for item in observed), include_finalized=False
    )
    if tuple(item.hash for item in evidence.blocks) != tuple(item.block_hash for item in observed):
        raise ScenarioFailure("Collection evidence is inconsistent with the indexed branch.")
    # An explicit empty block must still advance durable coverage without
    # creating a collection. Run the SAME Tier-0 session again to process it.
    await chain.rpc.call("evm_mine", [])
    head = await chain.current_head()
    await _follow(journal, chain, head.number)
    checkpoint = await observer.latest_checkpoint()
    if checkpoint.block != head or checkpoint.collection_count != 2:
        raise ScenarioFailure("Empty block did not advance coverage while retaining exactly two collections.")
    return _summary("collected", checkpoint, prior["snapshot"])


async def _restart(journal, chain, observer, _descriptor, prior):
    if prior is None or prior["phase"] != "collected":
        raise FixtureError("Restart must follow a completed real collection scan.")
    state = await observer.latest_checkpoint()
    _confirm(state, prior)
    # A separate disposable container invokes the real run_session against
    # the SAME PostgreSQL volume. Mine one new *empty* block so the concrete
    # advance_one() path must run after process restart, not merely return an
    # already complete checkpoint.
    await chain.rpc.call("evm_mine", [])
    head = await chain.current_head()
    if head.number <= state.block.number:
        raise ScenarioFailure("Restart fixture did not mine a new source block.")
    await _follow(journal, chain, head.number)
    after = await observer.latest_checkpoint()
    if (
        after.block != head
        or after.generation != state.generation
        or after.collection_count != state.collection_count
        or len(await _events(observer, after)) != 2
    ):
        raise ScenarioFailure("Restart did not scan the new empty block without duplicate financial events.")
    return _summary("restart", after, prior["snapshot"])


async def _reorg(journal, chain, observer, _descriptor, prior):
    if prior is None or prior["phase"] != "restart":
        raise FixtureError("Reorganization must follow the successful separate-session restart.")
    old = await observer.latest_checkpoint()
    _confirm(old, prior)
    if old.collection_count != 2:
        raise ScenarioFailure("Reorg fixture lacks the two actual indexed collection events.")
    before = await journal.stored_headers(chain.settings.start_block, old.block.number)
    # The snapshot was taken at baseline before creating any channel/collection.
    # Only the prefix through the saved snapshot height survives reorganization.
    # Find the saved snapshot boundary by its original source identity.
    # Deployment itself is always before the chosen snapshot.
    snapshot = prior["snapshot"]
    if await chain.rpc.call("evm_revert", [snapshot]) is not True:
        raise ScenarioFailure("Anvil did not confirm fixture snapshot restoration.")
    # Deliberately create a *different* branch at the old height before
    # expecting Tier-0 contradiction detection. Depth alone is not evidence.
    await chain.rpc.call("evm_increaseTime", [3600])
    for _ in range(64):
        head = await chain.current_head()
        if head.number >= old.block.number:
            break
        await chain.rpc.call("evm_mine", [])
    else:
        raise ScenarioFailure("Replacement branch exceeded the bounded reorg fixture depth.")
    if await chain.canonical(old.block):
        raise ScenarioFailure("Fixture did not produce a different canonical block at the old height.")
    try:
        await _follow(journal, chain, head.number)
    except BranchConflict:
        pass
    else:
        raise ScenarioFailure("Concrete Tier-0 scan did not reject the replaced checkpoint.")
    state = await journal.basis()
    if not state.blocked_reason:
        raise ScenarioFailure("Canonical contradiction was not durably invalidated.")
    try:
        await observer.voucher_collected_events(old, cursor=None, limit=1)
    except HistoryUnavailable:
        pass
    else:
        raise ScenarioFailure("Invalidated collection generation remained available.")
    # Select an exact retained ancestor still canonical in the restored branch.
    # No direct extension SQL or replacement scanner is permitted.
    survivors = [block for block in before if await chain.canonical(block)]
    if not survivors:
        raise ScenarioFailure("No retained canonical ancestor remains for checked manual repair.")
    anchor = survivors[-1]
    async with journal.writer() as writer:
        await writer.repair_to(await writer.basis(), anchor, chain)
    await _follow(journal, chain, head.number)
    recovered = await observer.latest_checkpoint()
    if (recovered.generation == old.generation
        or recovered.block.number < old.block.number
        or recovered.collection_count != 0
        or await _events(observer, recovered)):
        raise ScenarioFailure("Repaired branch did not reject orphaned collections or rotate generation.")
    return _summary("reorg", recovered, snapshot)


async def execute(phase):
    descriptor, prior = _input()
    settings = _settings(descriptor)
    if phase == "initialize":
        if prior is not None:
            raise FixtureError("Owner preparation is not a resume operation.")
        await initialize(os.environ["CONIX_TEST_OWNER_DSN"], settings)
        await initialize(os.environ["CONIX_TEST_OWNER_DSN"], settings)
        return None
    journal = Journal(os.environ["CONIX_TEST_DSN"], settings)
    async with open_chain(settings) as contract:
        chain = Chain(contract, settings)
        await chain.assert_source()
        observer = Observer(journal, chain)
        return await {
            "baseline": _baseline,
            "collected": _collect,
            "restart": _restart,
            "reorg": _reorg,
        }[phase](journal, chain, observer, descriptor, prior)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("initialize", "baseline", "collected", "restart", "reorg"))
    selected = parser.parse_args()
    try:
        summary = asyncio.run(execute(selected.phase))
    except Exception as error:
        # Explicit scenario failures have source-controlled, nonsecret reasons.
        # For arbitrary RPC/SQL/library errors, expose type and traceback
        # location only: untrusted exception values may contain credentials.
        print(phase_failure_record(selected.phase, error).decode("ascii"),
              file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    if summary is not None:
        print(json.dumps(summary, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
