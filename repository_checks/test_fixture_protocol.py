"""Host-only adversarial descriptor tests; no Docker, chain or signing secrets."""
import json
import unittest

from tools.fixtures.protocol import (
    FixtureError,
    ScenarioFailure,
    decode,
    parse_phase_failure,
    phase_failure_record,
)

IDENTITY = "conix-fixture01"
CONTRACT = "0x" + "12" * 20
ADDRESS = "0x" + "34" * 20
HASH = "0x" + "ab" * 32


def valid():
    """Build a syntactically valid, disposable v1 fixture descriptor."""
    event = [
        {"name": name, "type": type_, "indexed": indexed}
        for name, type_, indexed in (
            ("customer", "address", True), ("channelId", "uint256", True),
            ("payee", "address", True), ("amount", "uint256", False),
            ("voucherPaid", "uint256", False), ("invoiceHash", "bytes32", False),
        )
    ]
    functions = {
        "createChannel": ("address", "address", "uint64"),
        "setPayee": ("uint256", "address"),
        "registerVoucherKey": ("uint256", "address"),
        "fund": ("uint256", "uint256"),
        "collect": ("address", "uint256", "uint256", "bytes32", "uint256", "bytes"),
        "voucherDigest": ("address", "uint256", "uint256", "bytes32"),
        "channelCount": (),
        "viewPosition": ("address", "uint256", "address"),
    }
    abi = [
        {"type": "event", "name": "VoucherCollected", "anonymous": False, "inputs": event},
        *(dict(type="function", name=name, inputs=[dict(type=kind) for kind in kinds])
          for (name, kinds) in functions.items()),
    ]
    project = "dapps-pay-to-play-fixture-" + IDENTITY
    accounts = {
        name: {"address": "0x" + f"{index:02x}" * 20, "privateKey": "0x" + f"{index:02x}" * 32}
        for (index, name) in enumerate(("admin", "customer", "relayer", "payee"), 1)
    }
    contract = {"address": CONTRACT, "deploymentBlock": 2, "creationTransaction": HASH}
    return {
        "schema": "pay-to-play.local-fixture.v1", "fixtureId": IDENTITY,
        "composeProject": project, "dockerNetwork": project + "_pay-to-play_local",
        "rpcUrl": "http://pay-to-play-anvil:8545", "chainId": 31337,
        "contracts": {"CamRoot": contract.copy(),
                      "PayToPlay": contract | {"abi": abi},
                      "PayToPlayUI": contract.copy()},
        "publicFixtureAccounts": accounts,
    }


def encode(document):
    """Use the same single-line JSON framing as the private fixture handoff."""
    return (json.dumps(document, separators=(",", ":")) + "\n").encode()


class FixtureContractTests(unittest.TestCase):
    """Reject foreign source identities and secret-bearing public diagnostics."""

    def test_exact_dapp32_json_is_admitted(self):
        self.assertEqual(decode(encode(valid()), fixture_id=IDENTITY), valid())

    def test_borrowed_fixture_is_parsed_once_and_self_identifies(self):
        document = valid()
        self.assertEqual(decode(encode(document), fixture_id=None), document)
        document["fixtureId"] = "INVALID"
        with self.assertRaises(FixtureError):
            decode(encode(document), fixture_id=None)

    def test_wrong_source_address_network_schema_and_accounts_refused(self):
        for changed in (
            ("schema", "unsupported"), ("chainId", 1),
            ("rpcUrl", "http://attacker.invalid:8545"),
            ("dockerNetwork", "shared"), ("fixtureId", "other"),
            ("publicFixtureAccounts", {}),
        ):
            with self.subTest(field=changed[0]):
                document = valid()
                document[changed[0]] = changed[1]
                with self.assertRaises(FixtureError):
                    decode(encode(document), fixture_id=IDENTITY)

    def test_missing_selected_view_preconditions_are_refused_at_admission(self):
        """A real acceptance fixture needs both views before any SQL resources."""
        for missing in ("channelCount", "viewPosition"):
            fixture = valid()
            abi = fixture["contracts"]["PayToPlay"]["abi"]
            fixture["contracts"]["PayToPlay"]["abi"] = [
                entry for entry in abi if entry.get("name") != missing
            ]
            with self.subTest(missing=missing), self.assertRaises(FixtureError):
                decode(encode(fixture), fixture_id=IDENTITY)

    def test_wrong_abi_and_malformed_payloads_refused(self):
        data = valid()
        data["contracts"]["PayToPlay"]["abi"][0]["inputs"][3]["indexed"] = True
        with self.assertRaises(FixtureError):
            decode(encode(data), fixture_id=IDENTITY)
        baseline = encode(valid())
        for value in (b"", baseline + baseline, baseline + b"garbage", b'{"schema":1,"schema":2}'):
            with self.subTest(length=len(value)), self.assertRaises(FixtureError):
                decode(value, fixture_id=IDENTITY)

    def test_safe_scenario_cause_is_carried_with_exact_phase(self):
        original = ScenarioFailure("Generic acceptance differs from the real evidence")
        record = phase_failure_record("baseline", original)
        self.assertIn(
            "ScenarioFailure: Generic acceptance differs from the real evidence",
            parse_phase_failure(record, phase="baseline"),
        )
        self.assertIsNone(parse_phase_failure(record, phase="initialize"))
        self.assertIsNone(parse_phase_failure(
            b"CONIX_PHASE_FAILURE not-json", phase="baseline"
        ))

    def test_rpc_method_and_cleanup_uncertainty_survive_phase_record(self):
        primary = ScenarioFailure(
            "Anvil RPC eth_sendTransaction timed out; operation outcome unknown."
        )
        primary.add_note(
            "Anvil impersonation retirement failed (ScenarioFailure); "
            "the fixture state requires inspection."
        )

        record = phase_failure_record("exercise", primary)
        visible = parse_phase_failure(record, phase="exercise")

        self.assertIn("eth_sendTransaction timed out", visible)
        self.assertIn("impersonation retirement failed", visible)

        primary.add_note("privateKey=0x" + "aa" * 32)
        self.assertNotIn(b"privateKey", phase_failure_record("exercise", primary))

    def test_untrusted_exception_messages_cannot_reach_public_failure(self):
        fake_key = "0x" + "ab" * 32
        for original in (
            ValueError("postgresql://owner:password@private.example/" + fake_key),
            ScenarioFailure("privateKey=" + fake_key),
        ):
            with self.subTest(category=type(original).__name__):
                record = phase_failure_record("baseline", original)
                self.assertNotIn(fake_key.encode(), record)
                self.assertNotIn(b"password", record)
                explanation = parse_phase_failure(record, phase="baseline")
                self.assertIsNotNone(explanation)
                self.assertIn(type(original).__name__, explanation)
        # A child cannot bypass diagnostics admission by forging its stderr.
        forged = (
            b'CONIX_PHASE_FAILURE {"phase":"baseline","category":"ScenarioFailure",'
            b'"reason":"privateKey=0x' + b"ab" * 32 + b'"}'
        )
        self.assertIsNone(parse_phase_failure(forged, phase="baseline"))



if __name__ == "__main__":
    unittest.main()


class AcceptancePreflightContracts(unittest.TestCase):
    """Public source preflight cannot smuggle credentials to schema owner."""

    def test_exact_record_and_rejected_mutations(self):
        from tools.fixtures.protocol import acceptance_preflight
        safe = {
            "schema": "conix.acceptance.preflight.v1",
            "chainId": 31337,
            "genesisHash": "0x" + "ab" * 32,
        }
        self.assertEqual(acceptance_preflight((json.dumps(safe) + "\n").encode(), chain_id=31337), safe)
        for malformed in (
            {**safe, "chainId": 1},
            {**safe, "chainId": True},
            {**safe, "privateKey": "unacceptable"},
            {**safe, "genesisHash": "0x" + "aa" * 31},
            {**safe, "genesisHash": 0},
        ):
            with self.subTest(record=malformed), self.assertRaises(FixtureError):
                acceptance_preflight((json.dumps(malformed) + "\n").encode(), chain_id=31337)


class AcceptancePublicSourceContracts(unittest.TestCase):
    """Require an exact, credential-free schema-owner input."""

    def test_public_projection_and_admission(self):
        from tools.fixtures.protocol import public_acceptance_source, admit_public_acceptance_source
        fixture = valid()
        projected = public_acceptance_source(fixture)
        self.assertEqual(admit_public_acceptance_source(projected), projected)
        self.assertNotIn("publicFixtureAccounts", json.dumps(projected))
        self.assertNotIn("privateKey", json.dumps(projected))
        for mutation in (
            {**projected, "privateKey": "0x" + "99" * 32},
            {**projected, "chainId": True},
            {**projected, "rpcUrl": "https://untrusted.example"},
            {**projected, "payment": {"address": "0xdead", "deploymentBlock": 1}},
        ):
            with self.subTest(case=mutation), self.assertRaises(FixtureError):
                admit_public_acceptance_source(mutation)



class KeylessConsumerSourceContracts(unittest.TestCase):
    """Only the host operator needs the producer's public test private keys."""

    def test_projection_preserves_public_coordinates_without_wallet_material(self):
        from tools.fixtures.protocol import (
            admit_public_test_source, public_test_source,
        )

        private_descriptor = valid()
        projected = public_test_source(private_descriptor)
        self.assertEqual(
            admit_public_test_source(projected, fixture_id=IDENTITY),
            projected,
        )

        serialized = json.dumps(projected)
        self.assertNotIn("privateKey", serialized)
        for account in private_descriptor["publicFixtureAccounts"].values():
            self.assertNotIn(account["privateKey"], serialized)
        public_functions = projected["contracts"]["PayToPlay"]["abi"]
        self.assertTrue(
            all(row["type"] == "function" for row in public_functions),
        )
        self.assertEqual(
            {row["name"] for row in public_functions},
            {"voucherDigest", "channelCount", "viewPosition"},
        )
        self.assertEqual(
            projected["publicFixtureAccounts"]["customer"]["address"],
            private_descriptor["publicFixtureAccounts"]["customer"]["address"],
        )

    def test_unused_abi_metadata_cannot_carry_fixture_keys_to_children(self):
        from tools.fixtures.protocol import public_test_source

        descriptor = valid()
        fixture_key = descriptor["publicFixtureAccounts"]["customer"]["privateKey"]
        abi = descriptor["contracts"]["PayToPlay"]["abi"]

        # The producer's ABI contains arbitrary unrelated fields in this
        # adversarial case. They must not travel to the installed test child.
        abi[0]["metadata"] = {"operatorNote": fixture_key}
        voucher = next(row for row in abi if row.get("name") == "voucherDigest")
        voucher["privateKey"] = fixture_key
        voucher["inputs"][0]["unrelatedMetadata"] = fixture_key

        projected = public_test_source(descriptor)
        self.assertNotIn(fixture_key, json.dumps(projected))
        views = projected["contracts"]["PayToPlay"]["abi"]
        self.assertEqual(
            {view["name"] for view in views},
            {"channelCount", "voucherDigest", "viewPosition"},
        )
        self.assertEqual(
            next(view for view in views if view["name"] == "voucherDigest"),
            {
                "type": "function",
                "name": "voucherDigest",
                "inputs": [
                    {"name": "", "type": kind}
                    for kind in ("address", "uint256", "uint256", "bytes32")
                ],
                "outputs": [],
            },
        )

    def test_malformed_or_private_consumer_source_is_refused(self):
        from tools.fixtures.protocol import (
            admit_public_test_source, public_test_source,
        )

        projected = public_test_source(valid())
        invalid = (
            {**projected, "fixtureId": "foreign"},
            {**projected, "chainId": True},
            {**projected, "contracts": []},
            {**projected, "contracts": {"PayToPlay": {"address": "0xdead"}}},
            {**projected, "publicFixtureAccounts": valid()["publicFixtureAccounts"]},
            {**projected, "privateKey": "0x" + "55" * 32},
        )
        for candidate in invalid:
            with self.subTest(fields=set(candidate)), self.assertRaises(FixtureError):
                admit_public_test_source(candidate, fixture_id=IDENTITY)


class PaymentPhaseReceiptContracts(unittest.TestCase):
    """Only source and chain facts may pass from a test process to the next."""

    def test_exact_public_receipt_is_admitted(self):
        from tools.fixtures.protocol import acceptance_payments

        record = {
            "schema": "conix.acceptance.payments.v1",
            "checkpoint": {
                "source": "ab" * 32,
                "generation": "00000000-0000-0000-0000-000000000001",
                "start": 2,
                "revision": 7,
                "block": {
                    "number": 9, "hash": "cd" * 32,
                    "parent": "ef" * 32, "timestamp": 100,
                },
            },
            "transactions": ["0x" + f"{i:02x}" * 32 for i in range(4)],
            "collected": [30, 40],
        }
        self.assertEqual(acceptance_payments(encode(record)), record)

        mutations = (
            {**record, "privateKey": "0x" + "66" * 32},
            {**record, "transactions": [record["transactions"][0]] * 4},
            {**record, "checkpoint": {**record["checkpoint"], "source": "invalid"}},
            {**record, "checkpoint": {**record["checkpoint"], "start": 10}},
            {**record, "checkpoint": {**record["checkpoint"], "generation": "none"}},
            {**record, "collected": [30, -1]},
            {**record, "collected": [True, 40]},
            {**record, "collected": []},
            {**record, "channel": True},
        )
        for mutation in mutations:
            with self.subTest(fields=set(mutation)), self.assertRaises(FixtureError):
                acceptance_payments(encode(mutation))

        with self.assertRaises(FixtureError):
            acceptance_payments(b'{"schema":0,"schema":1}\n')



class TokenPhaseReceiptContracts(unittest.TestCase):
    """Deployment coordinates, never wallet data, cross the schema-owner boundary."""

    def test_two_distinct_deployed_contracts_are_required(self):
        from tools.fixtures.protocol import acceptance_tokens, admit_tokens

        record = {
            "schema": "conix.acceptance.tokens.v1",
            "token20": {
                "address": "0x" + "11" * 20,
                "deploymentBlock": 3,
                "creationTransaction": "0x" + "12" * 32,
            },
            "token721": {
                "address": "0x" + "22" * 20,
                "deploymentBlock": 4,
                "creationTransaction": "0x" + "23" * 32,
            },
        }
        self.assertEqual(acceptance_tokens(encode(record)), record)
        self.assertEqual(admit_tokens(record), record)
        self.assertIsNone(admit_tokens(None))

        mutants = (
            {**record, "privateKey": "0x" + "aa" * 32},
            {**record, "token721": record["token20"]},
            {**record, "token20": {**record["token20"], "deploymentBlock": True}},
            {**record, "token20": {**record["token20"], "creationTransaction": "wrong"}},
            {**record, "token721": {**record["token721"], "address": "0xdead"}},
        )
        for candidate in mutants:
            with self.subTest(mutation=candidate), self.assertRaises(FixtureError):
                acceptance_tokens(encode(candidate))
        with self.assertRaises(FixtureError):
            acceptance_tokens(b'{"schema":0,"schema":1}\n')
