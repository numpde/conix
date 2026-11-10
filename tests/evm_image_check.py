"""Check the installed EVM acceptance runner and its test-only Cast signer.

This module runs only in the evm-test-runner image, through unittest
discovery. Fixed public test vectors cannot disclose fixture account keys.
"""

import json
import httpx
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

import generic_acceptance
import t0_5_payment
import t0_5_chain


class EvmImageChecks(unittest.TestCase):

    def test_installed_acceptance_program(self) -> None:
        self.assertTrue(callable(generic_acceptance.main))

    def test_compiled_token_bytes_match_reviewed_solidity(self) -> None:
        from t0_5_tokens import token_creation_bytecode

        fixtures = token_creation_bytecode()
        self.assertEqual(set(fixtures), {"ConixToken20", "ConixToken721"})
        self.assertNotEqual(fixtures["ConixToken20"], fixtures["ConixToken721"])

    def test_cast_signs_and_verifies_fixed_prehashed_digest(self) -> None:
        digest = "0x" + "ab" * 32
        test_key = "0x" + "00" * 31 + "01"

        signed = subprocess.run(
            [
                "cast", "wallet", "sign", "--no-hash", digest,
                "--private-key", test_key,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(signed.returncode, 0, "Cast rejected the fixed public signing vector")
        actual_signature = signed.stdout.strip()
        self.assertEqual(len(actual_signature), 132)
        self.assertTrue(actual_signature.startswith("0x"))
        self.assertIn(bytes.fromhex(actual_signature[2:])[-1], (27, 28))

        # T0-5 uses wallet verify, not wallet sign, for the actual voucher.
        signer = subprocess.run(
            ["cast", "wallet", "address", "--private-key", test_key],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(signer.returncode, 0)
        verified = subprocess.run(
            [
                "cast", "wallet", "verify", "--no-hash",
                "--address", signer.stdout.strip(), digest, actual_signature,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, "Cast failed its actual T0-5 digest verification path")


class InstalledFixtureWiring(unittest.IsolatedAsyncioTestCase):
    """Exercise the fixture's actual ABI caller, not only module importability."""

    async def test_owner_and_keyless_consumer_compile_identical_three_contract_source(self):
        from conix.catalog import prepare_catalog

        genesis_hash = "0x" + "ab" * 32
        rpc_url = "http://pay-to-play-anvil:8545"
        payment_address = "0x" + "11" * 20
        tokens = {
            "token20": {"address": "0x" + "22" * 20},
            "token721": {"address": "0x" + "33" * 20},
        }
        consumer_source = {
            "chainId": 31337,
            "rpcUrl": rpc_url,
            "contracts": {
                "PayToPlay": {
                    "address": payment_address,
                    "deploymentBlock": 2,
                },
            },
        }
        owner_source = {
            "chainId": 31337,
            "rpcUrl": rpc_url,
            "payment": {
                "address": payment_address,
                "deploymentBlock": 2,
            },
        }

        consumer_catalog = prepare_catalog(
            generic_acceptance.catalog_path(consumer_source, genesis_hash, tokens),
        )
        owner_catalog = prepare_catalog(
            generic_acceptance.catalog_path(owner_source, genesis_hash, tokens),
        )

        self.assertEqual(owner_catalog.source_id, consumer_catalog.source_id)
        self.assertEqual(
            {contract.alias for contract in owner_catalog.contracts},
            {"payment", "token20", "token721"},
        )
        self.assertEqual(owner_catalog.chain.start_block, 2)
        self.assertEqual(
            {
                contract.alias: (
                    contract.descriptor.events[0].signature,
                    contract.descriptor.events[0].indexed_count,
                )
                for contract in owner_catalog.contracts
            },
            {
                "payment": (generic_acceptance.VOUCHER_SIGNATURE, 3),
                "token20": (generic_acceptance.TRANSFER_SIGNATURE, 2),
                "token721": (generic_acceptance.TRANSFER_SIGNATURE, 3),
            },
        )

    async def test_payment_view_uses_the_deployed_function_declaration(self):
        abi = json.loads(Path("/tests/fixtures/abi/pay_to_play.json").read_text())
        contract = "0x" + "12" * 20
        expected_asset = "0x" + "34" * 20
        descriptor = {"contracts": {"PayToPlay": {"address": contract, "abi": abi}}}

        async def observed_rpc(_descriptor, method, params):
            self.assertEqual(method, "eth_call")
            self.assertEqual(params[0]["to"], contract)
            self.assertEqual(params[0]["data"], "0x" + keccak(text="asset()")[:4].hex())
            self.assertEqual(params[1], "latest")
            return "0x" + encode(("address",), (expected_asset,)).hex()

        result = await t0_5_payment.payment_call(
            observed_rpc, descriptor, "asset", (),
        )
        self.assertEqual(result, (to_checksum_address(expected_asset),))

    async def test_rpc_failure_uses_method_and_code_not_remote_message(self):
        secret = "privateKey=0x" + "ab" * 32
        url = "http://fixture.invalid:8545"
        request = httpx.Request("POST", url)
        reply = httpx.Response(200, json={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": secret},
        }, request=request)

        class SelectedTransport:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, *_args, **_kwargs):
                return reply

        with patch.object(generic_acceptance.httpx, "AsyncClient", return_value=SelectedTransport()):
            with self.assertRaises(generic_acceptance.ScenarioFailure) as raised:
                await generic_acceptance.rpc({"rpcUrl": url}, "eth_call", [])

        self.assertIn("eth_call", str(raised.exception))
        self.assertIn("-32000", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    async def test_rpc_timeout_preserves_unknown_mutation_outcome(self):
        url = "http://fixture.invalid:8545"
        request = httpx.Request("POST", url)

        class TimedOutTransport:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, *_args, **_kwargs):
                raise httpx.ReadTimeout("secret://upstream-password", request=request)

        with patch.object(generic_acceptance.httpx, "AsyncClient", return_value=TimedOutTransport()):
            with self.assertRaises(generic_acceptance.ScenarioFailure) as raised:
                await generic_acceptance.rpc({"rpcUrl": url}, "eth_sendTransaction", [])

        self.assertIn("eth_sendTransaction", str(raised.exception))
        self.assertIn("outcome unknown", str(raised.exception))
        self.assertNotIn("secret://", str(raised.exception))

    async def test_token_creation_submits_bytecode_without_a_destination(self):
        calls = []
        creator = "0x" + "34" * 20
        expected_hash = "0x" + "ab" * 32

        async def observed_rpc(_descriptor, method, params):
            calls.append(method)
            if method == "eth_sendTransaction":
                self.assertEqual(params, [{
                    "data": "0x6000",
                    "gas": "0x300000",
                    "from": creator,
                }])
                return expected_hash
            return None  # Anvil impersonation and retirement succeed with JSON null.

        transaction_hash = await t0_5_chain.submit_test_transaction(
            observed_rpc,
            {},
            creator,
            {"data": "0x6000", "gas": "0x300000"},
            wait=False,
        )
        self.assertEqual(transaction_hash, expected_hash)
        self.assertEqual(
            calls,
            ["anvil_impersonateAccount", "eth_sendTransaction",
             "anvil_stopImpersonatingAccount"],
        )

    async def test_uncertain_automine_start_still_attempts_restoration(self):
        changes = []

        async def observed_rpc(_descriptor, method, params):
            changes.append((method, params))
            if method == "anvil_setAutomine" and params == [False]:
                raise generic_acceptance.ScenarioFailure(
                    "Anvil RPC anvil_setAutomine timed out; operation outcome unknown."
                )
            return None

        with self.assertRaises(generic_acceptance.ScenarioFailure):
            await t0_5_payment.mine_mixed_block(
                observed_rpc,
                {},
                {},
                signer="0x" + "11" * 20,
                customer="0x" + "22" * 20,
                collector="0x" + "33" * 20,
                invoice=bytes(32),
            )

        self.assertEqual(
            changes,
            [("anvil_setAutomine", [False]), ("anvil_setAutomine", [True])],
        )

    async def test_uncertain_impersonation_start_still_attempts_retirement(self):
        operations = []

        async def observed_rpc(_descriptor, method, _params):
            operations.append(method)
            if method == "anvil_impersonateAccount":
                raise generic_acceptance.ScenarioFailure(
                    "Anvil RPC anvil_impersonateAccount timed out; operation outcome unknown."
                )
            return True

        with self.assertRaises(generic_acceptance.ScenarioFailure) as raised:
            await t0_5_chain.submit_test_transaction(
                observed_rpc,
                {},
                "0x" + "34" * 20,
                {"data": "0x6000"},
                wait=False,
            )

        self.assertEqual(
            operations,
            ["anvil_impersonateAccount", "anvil_stopImpersonatingAccount"],
        )
        self.assertIn("outcome unknown", str(raised.exception))

    async def test_failed_submission_keeps_cause_when_impersonation_cleanup_fails(self):
        operations = []

        async def observed_rpc(_descriptor, method, _params):
            operations.append(method)
            if method == "eth_sendTransaction":
                raise ValueError("submission rejected by the selected Anvil")
            if method == "anvil_stopImpersonatingAccount":
                raise RuntimeError("retirement unavailable")
            return True

        with self.assertRaises(ValueError) as raised:
            await t0_5_chain.send(
                observed_rpc,
                {"contracts": {"PayToPlay": {"address": "0x" + "12" * 20}}},
                "0x" + "34" * 20,
                "asset()",
                (),
                (),
            )

        self.assertEqual(
            operations,
            ["anvil_impersonateAccount", "eth_sendTransaction",
             "anvil_stopImpersonatingAccount"],
        )
        self.assertIn("retirement failed", " ".join(raised.exception.__notes__))
