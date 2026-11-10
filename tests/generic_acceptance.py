"""Opt-in RED generic Tier-0 acceptance, in the installed EVM test image.

Uses the existing dapp32-owned Anvil fixture and Conix-owned PostgreSQL.
No alternate scanner, mocked reader, silent skip or legacy observer fallback.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_protocol import (FixtureError, ScenarioFailure, _bad_constant, _pairs,
                              admit_public_acceptance_source, admit_tokens, decode, phase_failure_record)
from abi_golden_vectors import ERC20, ERC721
from abi_contract_vectors import PAY_TO_PLAY, COMPOUND
import httpx
from psycopg import AsyncConnection


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
    return decode((json.dumps(descriptor, separators=(",", ":")) + "\n").encode(), fixture_id=fixture_id), document["state"], tokens


async def rpc(descriptor, method, params):
    async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
        response = await client.post(descriptor["rpcUrl"], json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
        response.raise_for_status()
        result = response.json()
    if type(result) is not dict or result.get("id") != 1 or "error" in result or "result" not in result:
        raise ScenarioFailure("Real Anvil returned an invalid RPC observation.")
    return result["result"]


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
                'events = ["Transfer(address,address,uint256)"]\n'
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
        'events = ["VoucherCollected(address,uint256,address,uint256,uint256,bytes32)"]\n'
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


class InstalledAcceptance(unittest.IsolatedAsyncioTestCase):

    async def test_00_real_rpc_and_deployed_code(self):
        self.assertEqual((await genesis(self.descriptor)).lower(), self.genesis_hash.lower())
        code = await rpc(self.descriptor, "eth_getCode", [
            self.descriptor["contracts"]["PayToPlay"]["address"], "latest",
        ])
        self.assertTrue(type(code) is str and code.startswith("0x") and len(code) > 2)

    async def test_01_runtime_has_no_schema_ownership(self):
        async with await AsyncConnection.connect(os.environ["CONIX_TEST_DSN"], connect_timeout=5) as connection:
            result = await (await connection.execute(
                "SELECT current_user, has_schema_privilege(current_user, 'conix', 'CREATE')"
            )).fetchone()
        self.assertEqual(result, ("conix_runtime", False))

    async def test_02_abi_codec_matches_independent_wire_vectors(self):
        from conix.abi import compile_event, decode_event
        for vector in (ERC20, ERC721, PAY_TO_PLAY, COMPOUND):
            with self.subTest(vector=vector.name):
                compiled = compile_event(vector.abi)
                self.assertEqual(
                    tuple(decode_event(compiled, vector.topics, vector.data)), vector.expected,
                )

    async def test_03_complete_block_publisher_and_empty_filtered_page(self):
        from conix.catalog import prepare_catalog
        from conix.core import open_writer
        from conix.reader import open_reader
        source = prepare_catalog(self.catalog)
        async with open_writer(catalog=source, database_dsn=os.environ["CONIX_TEST_DSN"],
                               rpc_url=self.descriptor["rpcUrl"]) as writer:
            await writer.advance_one()
        async with open_reader(catalog=source, database_dsn=os.environ["CONIX_TEST_READ_DSN"],
                               rpc_url=self.descriptor["rpcUrl"]) as reader:
            checkpoint = await reader.checkpoint()
            self.assertGreaterEqual(
                checkpoint.block.number, self.descriptor["contracts"]["PayToPlay"]["deploymentBlock"],
            )
            page = await reader.events(
                checkpoint=checkpoint, contract="payment",
                event="VoucherCollected(address,uint256,address,uint256,uint256,bytes32)",
                where={"customer": self.descriptor["publicFixtureAccounts"]["customer"]["address"]},
                cursor=None, limit=100,
            )
            self.assertEqual(page.checkpoint, checkpoint)
            self.assertTrue(page.complete)
            self.assertEqual(page.events, ())

    async def test_04_raw_event_bytea_and_owner_boundary(self):
        async with await AsyncConnection.connect(os.environ["CONIX_TEST_DSN"], connect_timeout=5) as connection:
            value = await (await connection.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema='conix' AND table_name='events' AND column_name='data'"
            )).fetchone()
        self.assertEqual(value, ("bytea",))

    async def test_05_checkpoint_hash_pinned_view(self):
        from conix.catalog import prepare_catalog
        from conix.reader import open_reader
        source = prepare_catalog(self.catalog)
        async with open_reader(catalog=source, database_dsn=os.environ["CONIX_TEST_READ_DSN"],
                               rpc_url=self.descriptor["rpcUrl"]) as reader:
            checkpoint = await reader.checkpoint()
            result = await reader.call(
                checkpoint=checkpoint, contract="payment", function="asset()", arguments=(),
            )
            self.assertIs(type(result), tuple)
            self.assertEqual(len(result), 1)


async def baseline(descriptor, state, tokens):
    expected = preflight(state, descriptor)
    if (await genesis(descriptor)).lower() != expected.lower():
        raise ScenarioFailure("Anvil genesis changed after owner initialization.")
    InstalledAcceptance.descriptor = descriptor
    InstalledAcceptance.genesis_hash = expected
    InstalledAcceptance.catalog = catalog_path(descriptor, expected, tokens)
    sink = io.StringIO()
    tests = unittest.defaultTestLoader.loadTestsFromTestCase(InstalledAcceptance)
    result = unittest.TextTestRunner(stream=sink, verbosity=0).run(tests)
    for test, _ in sorted(result.failures + result.errors, key=lambda row: str(row[0])):
        print("FAIL:", str(test))
    if not result.wasSuccessful():
        raise ScenarioFailure(
            f"{len(result.failures) + len(result.errors)} named generic acceptance checks failed."
        )
    print(f"PASS: {result.testsRun} installed generic Tier-0 acceptance checks.")


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
            from t0_5_tokens import deploy
            print(json.dumps(await deploy(descriptor, rpc), separators=(",", ":")))
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
    except BaseException as error:
        safe_error = error if isinstance(error, Exception) else ScenarioFailure("Acceptance interrupted.")
        print(phase_failure_record(phase, safe_error).decode("ascii"), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
