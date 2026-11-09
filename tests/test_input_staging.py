"""Input-staging protocol and strict secret-envelope bounds; no Docker required."""
import struct
import unittest

from conix.input_staging import MAGIC, MAX_CONFIG, MAX_DSN, InputError, decode


class InputEnvelopeTests(unittest.TestCase):
    """Prove exact byte preservation and fail-closed secret envelope parsing."""

    def payload(self, config=None, secret=b"postgresql://example.invalid/db"):
        if config is None:
            config = b'[database]\ndsn_file="/run/conix/connection.dsn"\n'
        return MAGIC + struct.pack("!II", len(config), len(secret)) + config + secret

    def test_valid_payload_keeps_exact_bytes(self):
        config = b'[database]\ndsn_file="/run/conix/connection.dsn"\n'
        secret = b"postgresql://test:private@postgres/db\n"
        self.assertEqual(decode(self.payload(config, secret)), (config, secret))

    def test_refuses_truncation_trailing_bytes_and_wrong_version(self):
        original = self.payload()
        for malformed in (
            original[:-1], original + b"X", b"unknown" + original,
            MAGIC + struct.pack("!II", MAX_CONFIG + 1, 4),
            MAGIC + struct.pack("!II", 4, MAX_DSN + 1),
        ):
            with self.subTest(length=len(malformed)), self.assertRaises(InputError):
                decode(malformed)

    def test_refuses_other_credential_location(self):
        with self.assertRaises(InputError):
            decode(self.payload(b'[database]\ndsn_file="/tmp/secret"\n'))

    def test_refuses_empty_or_nul_credential(self):
        for secret in (b"", b"postgresql://foo\0bar"):
            with self.subTest(secret=secret), self.assertRaises(InputError):
                decode(self.payload(secret=secret))
