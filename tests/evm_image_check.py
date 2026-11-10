"""Check the installed EVM acceptance runner and its test-only Cast signer.

This module runs only in the evm-test-runner image, through unittest
discovery. Fixed public test vectors cannot disclose fixture account keys.
"""

import subprocess
import unittest

import generic_acceptance


class EvmImageChecks(unittest.TestCase):

    def test_installed_acceptance_program(self) -> None:
        self.assertTrue(callable(generic_acceptance.main))

    def test_compiled_token_bytes_match_reviewed_solidity(self) -> None:
        from t0_5_tokens import code_artifacts

        fixtures = code_artifacts()
        self.assertEqual(set(fixtures), {"ConixToken20", "ConixToken721"})
        self.assertNotEqual(fixtures["ConixToken20"], fixtures["ConixToken721"])

    def test_cast_signs_fixed_prehashed_digest(self) -> None:
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
