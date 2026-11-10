"""Host-only adversarial descriptor tests; no Docker, chain or signing secrets."""
import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from fixture_protocol import (FixtureError, ScenarioFailure, decode, parse_phase_failure,
                              phase_failure_record)

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
        from fixture_protocol import acceptance_preflight
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
        from fixture_protocol import public_acceptance_source, admit_public_acceptance_source
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
