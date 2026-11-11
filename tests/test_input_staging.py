"""Input-staging protocol and strict secret-envelope bounds; no Docker required."""
import struct
import unittest

from conix.input_staging import MAGIC, MAX_CONFIG, MAX_DSN, InputError, decode


class InputEnvelopeTests(unittest.TestCase):
    """Prove exact byte preservation and fail-closed secret envelope parsing."""

    def payload(self, config=None, secret=b"postgresql://example.invalid/db", members=None):
        if config is None:
            config = b'[database]\ndsn_file="/run/conix/connection.dsn"\n'
        if members is None:
            members = [(b"test.json", b"[ ]\n")]
        return (
            MAGIC + struct.pack("!III", len(config), len(secret), len(members)) + config + secret
            + b"".join(struct.pack("!HI", len(name), len(content)) + name + content
                       for (name, content) in members)
        )

    def test_valid_payload_keeps_exact_bytes(self):
        config = b'[database]\ndsn_file="/run/conix/connection.dsn"\n'
        secret = b"postgresql://test:private@postgres/db\n"
        self.assertEqual(decode(self.payload(config, secret)), (config, secret, {"test.json": b"[ ]\n"}))

    def test_refuses_truncation_trailing_bytes_and_wrong_version(self):
        original = self.payload()
        for malformed in (
            original[:-1], original + b"X", b"unknown" + original,
            MAGIC + struct.pack("!III", MAX_CONFIG + 1, 4, 1),
            MAGIC + struct.pack("!III", 4, MAX_DSN + 1, 1),
        ):
            with self.subTest(length=len(malformed)), self.assertRaises(InputError):
                decode(malformed)

    def test_transport_does_not_validate_catalog_meaning(self):
        config = b'[database]\ndsn_file="/tmp/secret"\n'
        self.assertEqual(decode(self.payload(config))[0], config)
        malformed_toml = b"not a TOML document"
        self.assertEqual(decode(self.payload(malformed_toml))[0], malformed_toml)

    def test_refuses_unsafe_duplicate_or_empty_abi_members(self):
        for members in (
            [], [(b"../test.json", b"[]")], [(b"/test.json", b"[]")],
            [(b"test.json", b"")], [(b"test.json", b"[]"), (b"test.json", b"[]")],
        ):
            with self.subTest(members=members), self.assertRaises(InputError):
                decode(self.payload(members=members))

    def test_refuses_empty_or_nul_credential(self):
        for secret in (b"", b"postgresql://foo\0bar"):
            with self.subTest(secret=secret), self.assertRaises(InputError):
                decode(self.payload(secret=secret))

