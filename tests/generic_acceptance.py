"""Opt-in installed Conix acceptance against the dapp32-owned Anvil fixture.

The owner-only schema phase and runtime/reader phases share one immutable
source catalog. Test transactions and chain mutation belong to the owned
campaign; the borrowed baseline remains non-destructive.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_protocol import (FixtureError, ScenarioFailure, _bad_constant, _pairs,
                              admit_public_acceptance_source, admit_public_test_source, admit_tokens,
                              phase_failure_record)
from abi_contract_vectors import VOUCHER_SIGNATURE
from abi_golden_vectors import TRANSFER_SIGNATURE
import httpx


def request():
    raw = sys.stdin.buffer.read(1048577)
    if not 0 < len(raw) <= 1048576 or len(raw.splitlines()) != 1:
        raise FixtureError("Generic acceptance requires one bounded private request.")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise FixtureError("Generic acceptance request is invalid JSON.") from None
    if type(document) is not dict or set(document) != {"descriptor", "state", "tokens"}:
        raise FixtureError("Generic acceptance request must contain descriptor and phase state.")
    tokens = admit_tokens(document["tokens"])
    descriptor = document["descriptor"]
    if type(descriptor) is not dict:
        raise FixtureError("Generic acceptance descriptor is malformed.")
    if sys.argv[1] == "initialize":
        return admit_public_acceptance_source(descriptor), document["state"], tokens

    fixture_id = os.environ.get("CONIX_FIXTURE_ID", "")
    return admit_public_test_source(descriptor, fixture_id=fixture_id), document["state"], tokens


async def rpc(descriptor, method, params):
    """Observe the selected Anvil without disclosing requests or upstream prose.

    A lost response to a mutating method does not prove that its transaction
    or chain change was rejected. The owned campaign must reconcile the chain,
    not automatically repeat the request.
    """
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            response = await client.post(
                descriptor["rpcUrl"],
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            document = response.json()
    except httpx.TimeoutException as error:
        raise ScenarioFailure(f"Anvil RPC {method} timed out; operation outcome unknown.") from error
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        raise ScenarioFailure(f"Anvil RPC {method} returned HTTP {status}; operation outcome unknown.") from error
    except httpx.RequestError as error:
        category = type(error).__name__
        raise ScenarioFailure(f"Anvil RPC {method} transport failed ({category}); outcome unknown.") from error
    except ValueError as error:
        raise ScenarioFailure(f"Anvil RPC {method} returned malformed JSON.") from error

    if type(document) is not dict or document.get("jsonrpc") != "2.0" or document.get("id") != 1:
        raise ScenarioFailure(f"Anvil RPC {method} returned a mismatched response.")

    if "error" in document:
        failure = document["error"]
        code = failure.get("code") if type(failure) is dict else None
        if type(code) is int and -2**31 <= code < 2**31:
            raise ScenarioFailure(f"Anvil RPC {method} rejected the request with code {code}.")
        raise ScenarioFailure(f"Anvil RPC {method} rejected the request without a valid code.")

    if "result" not in document:
        raise ScenarioFailure(f"Anvil RPC {method} omitted its result.")

    return document["result"]

def is_hash(value):
    return (
        type(value) is str and len(value) == 66 and value.startswith("0x")
        and all(c in "0123456789abcdefABCDEF" for c in value[2:])
    )


async def genesis(descriptor):
    chain = await rpc(descriptor, "eth_chainId", [])
    block = await rpc(descriptor, "eth_getBlockByNumber", ["0x0", False])
    if chain != hex(descriptor["chainId"]) or type(block) is not dict or block.get("number") != "0x0":
        raise ScenarioFailure("Real Anvil chain/genesis does not match the selected fixture.")
    if not is_hash(block.get("hash")):
        raise ScenarioFailure("Real Anvil supplied an invalid genesis hash.")
    return block["hash"]


def preflight(state, descriptor):
    if (
        type(state) is not dict or set(state) != {"schema", "chainId", "genesisHash"}
        or state["schema"] != "conix.acceptance.preflight.v1"
        or type(state["chainId"]) is not int or state["chainId"] != descriptor["chainId"]
        or not is_hash(state["genesisHash"])
    ):
        raise ScenarioFailure("Missing independently checked source preflight.")
    return state["genesisHash"]


def catalog_path(descriptor, genesis_hash, tokens=None):
    """Stage only verified public ABI and source metadata in contained tmpfs."""
    root = Path("/tmp/conix-acceptance")
    root.mkdir(mode=0o700, exist_ok=True)
    # Both owner and reader use one reviewed ABI snapshot bundled in the
    # installed test image, not data from the private fixture handoff.
    reviewed_abi = Path("/tests/fixtures/abi/pay_to_play.json")
    payment_abi = root / "payment.json"
    payment_abi.write_bytes(reviewed_abi.read_bytes())
    payment_abi.chmod(0o600)
    deployment = (
        descriptor["payment"] if "payment" in descriptor
        else descriptor["contracts"]["PayToPlay"]
    )
    extra_contracts = ""
    if tokens is not None:
        for alias in ("token20", "token721"):
            name = "erc20" if alias == "token20" else "erc721"
            abi = root / (alias + ".json")
            abi.write_bytes((Path("/tests/fixtures/abi") / (name + ".json")).read_bytes())
            abi.chmod(0o600)
            extra_contracts += (
                f'[contracts.{alias}]\n'
                f'address = "{tokens[alias]["address"]}"\n'
                f'abi_file = "{abi}"\n'
                f'events = [{json.dumps(TRANSFER_SIGNATURE)}]\n'
            )
    config = root / "source.toml"
    config.write_text(
        'version = 1\n[chain]\n'
        f'chain_id = {descriptor["chainId"]}\n'
        f'genesis_hash = "{genesis_hash}"\n'
        f'start_block = {deployment["deploymentBlock"]}\n'
        f'rpc_url = {json.dumps(descriptor["rpcUrl"])}\n'
        '[contracts.payment]\n'
        f'address = "{deployment["address"]}"\n'
        f'abi_file = "{payment_abi}"\n'
        f'events = [{json.dumps(VOUCHER_SIGNATURE)}]\n'
        + extra_contracts
        + '[database]\ndsn_file = "/run/conix/connection.dsn"\nruntime_role = "conix_runtime"\n'
        '[limits]\nrpc_timeout_seconds = 5\noperation_timeout_seconds = 30\n'
        'max_response_bytes = 1048576\nmax_connections = 4\nmax_page_size = 100\n'
        'max_evidence_blocks = 64\nmax_logs_per_block = 10000\n'
        'max_abi_bytes = 1048576\nmax_abi_nesting = 8\nmax_abi_elements = 4096\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config


async def initialize(descriptor, state, tokens):
    source = catalog_path(descriptor, preflight(state, descriptor), tokens)
    try:
        from conix.catalog import prepare_catalog
    except ImportError:
        raise ScenarioFailure("Generic Tier-0 catalog admission is not installed.") from None
    from conix.schema import initialize as initialize_schema
    await initialize_schema(os.environ["CONIX_TEST_OWNER_DSN"], prepare_catalog(source))


async def baseline(descriptor, state, tokens):
    """Exercise the installed scanner and reader against one known source.

    The selected event-history origin is the actual PayToPlay deployment.
    Publishing the first block is a prerequisite of the following reads,
    not an accidental side effect of another unittest method.
    """
    from conix.catalog import prepare_catalog
    from conix.core import open_writer
    from conix.reader import open_reader

    expected_genesis = preflight(state, descriptor)
    observed_genesis = await genesis(descriptor)
    if observed_genesis.lower() != expected_genesis.lower():
        raise ScenarioFailure("Anvil genesis changed after owner initialization.")

    deployed = {"payment": descriptor["contracts"]["PayToPlay"]["address"]}
    if tokens is not None:
        deployed.update(
            token20=tokens["token20"]["address"],
            token721=tokens["token721"]["address"],
        )

    for (alias, address) in deployed.items():
        code = await rpc(descriptor, "eth_getCode", [address, "latest"])
        if type(code) is not str or not code.startswith("0x") or len(code) <= 2:
            raise ScenarioFailure(f"{alias} deployment has no code on the selected chain.")

    catalog = prepare_catalog(catalog_path(descriptor, expected_genesis, tokens))
    async with open_writer(
        catalog=catalog,
        database_dsn=os.environ["CONIX_TEST_DSN"],
        rpc_url=descriptor["rpcUrl"],
    ) as writer:
        published = await writer.advance_one()
        if published is None or published.last_block is None:
            raise ScenarioFailure("The initial complete block was not published.")

    async with open_reader(
        catalog=catalog,
        database_dsn=os.environ["CONIX_TEST_READ_DSN"],
        rpc_url=descriptor["rpcUrl"],
    ) as reader:
        checkpoint = await reader.checkpoint()
        if checkpoint is None or checkpoint.block != published.last_block:
            raise ScenarioFailure("The reader did not observe the published checkpoint.")

        page = await reader.events(
            checkpoint=checkpoint,
            contract="payment",
            event=VOUCHER_SIGNATURE,
            where={"customer": descriptor["publicFixtureAccounts"]["customer"]["address"]},
            limit=100,
        )
        if page.checkpoint != checkpoint or not page.complete or page.events:
            raise ScenarioFailure("A fresh payment deployment yielded contradictory event history.")

        native_asset = await reader.call(
            checkpoint=checkpoint,
            contract="payment",
            function="asset()",
            arguments=(),
        )
        from eth_utils import to_checksum_address
        expected_asset = to_checksum_address("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")
        if native_asset != (expected_asset,):
            raise ScenarioFailure("The pinned historical asset view differs from the deployed contract.")

    print("PASS: installed scanner and SELECT-only reader share the first complete checkpoint.")


async def main():
    if len(sys.argv) != 2 or sys.argv[1] not in (
        "preflight", "prepare", "initialize", "baseline", "exercise", "resume", "reorg",
    ):
        raise FixtureError("Select an explicit generic acceptance phase.")
    phase = sys.argv[1]
    try:
        descriptor, state, tokens = request()
        if phase == "preflight":
            if state is not None:
                raise FixtureError("Preflight cannot consume prior state.")
            print(json.dumps({
                "schema": "conix.acceptance.preflight.v1",
                "chainId": descriptor["chainId"],
                "genesisHash": await genesis(descriptor),
            }, separators=(",", ":")))
        elif phase == "prepare":
            expected_genesis = preflight(state, descriptor)
            if (await genesis(descriptor)).lower() != expected_genesis.lower():
                raise ScenarioFailure("Selected Anvil changed after fixture source preflight.")
            if tokens is not None:
                raise ScenarioFailure("Contract fixture preparation must start with no token source.")
            from t0_5_tokens import deploy_token_fixtures
            print(json.dumps(await deploy_token_fixtures(descriptor, rpc), separators=(",", ":")))
        elif phase == "initialize":
            await initialize(descriptor, state, tokens)
        elif phase == "baseline":
            await baseline(descriptor, state, tokens)
        else:
            # Every phase is a distinct Docker process with the same journal.
            # Only the owner campaign selects these test-only chain mutations.
            import t0_5_payment

            if tokens is None:
                raise ScenarioFailure("Real multi-contract acceptance lacks its installed token source.")
            source = catalog_path(descriptor, await genesis(descriptor), tokens)
            arguments = {
                "writer_dsn": os.environ["CONIX_TEST_DSN"],
                "reader_dsn": os.environ["CONIX_TEST_READ_DSN"],
            }
            if phase == "exercise":
                preflight(state, descriptor)
                record = await t0_5_payment.exercise(
                    descriptor, source, rpc, tokens=tokens, **arguments,
                )
                print(json.dumps(record, separators=(",", ":")))
            elif phase == "resume":
                await t0_5_payment.resume(
                    descriptor, source, rpc, state=state, tokens=tokens, **arguments,
                )
            else:
                await t0_5_payment.reorganize(
                    descriptor, source, rpc, state=state, tokens=tokens, **arguments,
                )
    except Exception as error:
        # Cancellation and keyboard interruption are not failed assertions.
        # Preserve their signal semantics; the host operator owns cleanup.
        print(phase_failure_record(phase, error).decode("ascii"), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
