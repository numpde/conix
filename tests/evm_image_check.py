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
from eth_utils import keccak

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

    def test_cast_verifies_a_fixed_public_digest_signature(self) -> None:
        """Prove the actual no-hash verifier without any wallet key in argv."""
        digest = "0x" + "ab" * 32
        signer = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
        signature_words = (
            "6170354e527d7bb63343788c1b7b317b69cdc5ddb05e7e93aa4c1b1635e55be"
            "4420ff721da6f5b2cbf49a7c5194264e47e1da3db875ea6cffe02a88e9d6ebf37"
        )
        # A recovered secp256k1 signature has one of two recovery parities.
        # Both candidate signatures are fixed, public test vectors.
        successful_parities = []
        for parity in (27, 28):
            verified = subprocess.run(
                [
                    "cast", "wallet", "verify", "--no-hash", "--address",
                    signer, digest, "0x" + signature_words + f"{parity:02x}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if verified.returncode == 0:
                successful_parities.append(parity)

        self.assertEqual(len(successful_parities), 1)


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

    async def test_projected_deployed_tuple_remains_callable_and_named(self):
        """The exact viewPosition tuple survives the keyless source boundary."""
        from eth_utils.abi import collapse_if_tuple
        from fixture_protocol import public_test_source

        reviewed_abi = json.loads(Path("/tests/fixtures/abi/pay_to_play.json").read_text())
        customer = "0x" + "34" * 20
        payment_address = "0x" + "12" * 20
        descriptor = {
            "fixtureId": "conix-example",
            "chainId": 31337,
            "rpcUrl": "http://pay-to-play-anvil:8545",
            "contracts": {
                "PayToPlay": {
                    "address": payment_address,
                    "deploymentBlock": 2,
                    "abi": reviewed_abi,
                },
            },
            "publicFixtureAccounts": {
                name: {"address": "0x" + f"{ordinal:02x}" * 20}
                for (ordinal, name) in enumerate(
                    ("admin", "customer", "relayer", "payee"), start=1,
                )
            },
        }
        selected = public_test_source(descriptor)
        position_abi = next(
            entry for entry in selected["contracts"]["PayToPlay"]["abi"]
            if entry["name"] == "viewPosition"
        )
        output = position_abi["outputs"][0]
        values = []
        for component in output["components"]:
            name = component["name"]
            kind = component["type"]
            if kind == "address":
                values.append(customer)
            elif kind == "string[]":
                values.append(["collect"])
            elif kind.startswith("uint"):
                values.append({"channelId": 1, "balance": 70,
                               "voucherPaid": 30, "readyAt": 90}.get(name, 0))
            else:
                self.fail(f"Unexpected declared position field type {kind!r}")

        encoded = encode((collapse_if_tuple(output),), (tuple(values),))

        async def observed_rpc(_descriptor, method, params):
            self.assertEqual(method, "eth_call")
            self.assertEqual(params[0]["to"], payment_address)
            self.assertTrue(params[0]["data"].startswith(
                "0x" + keccak(text="viewPosition(address,uint256,address)")[:4].hex()
            ))
            self.assertEqual(params[1], "latest")
            return "0x" + encoded.hex()

        (fields,) = await t0_5_payment.payment_call(
            observed_rpc,
            selected,
            "viewPosition",
            (customer, 1, customer),
        )
        position = t0_5_payment.named_position(selected, fields)
        self.assertEqual((position["channelId"], position["balance"],
                          position["voucherPaid"], position["readyAt"]),
                         (1, 70, 30, 90))
        self.assertEqual(position["actions"], ("collect",))

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
