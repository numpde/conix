"""Developer source-expression notes without argument labels or value leaks."""

import unittest

from conix.catalog import ConfigurationError, _quantity as catalog_quantity
from conix.core import _bytes, _quantity
from conix.rpc import RpcProtocolError


class ExpressionDiagnostics(unittest.TestCase):

    def test_one_line_calls_are_disambiguated_by_instruction_position(self):
        response = {"blockHash": None, "address": None}

        for method in (
            lambda: _bytes(response.get("blockHash"), 32),
            lambda: _bytes(response.get("address"), 20),
        ):
            with self.assertRaises(RpcProtocolError) as rejected:
                method()

            notes = rejected.exception.__notes__
            self.assertEqual(len(notes), 1)
            self.assertIn("response.get(", notes[0])

        self.assertIn('"blockHash"', (
            self._failure(lambda: _bytes(response.get("blockHash"), 32))
        ).__notes__[0])
        self.assertIn('"address"', (
            self._failure(lambda: _bytes(response.get("address"), 20))
        ).__notes__[0])

    @staticmethod
    def _failure(operation):
        try:
            operation()
        except RpcProtocolError as error:
            return error
        raise AssertionError("Expected the RPC converter to refuse its input.")

    def test_quantity_conversion_and_config_use_calling_expression(self):
        response = {"blockNumber": "-1"}
        with self.assertRaises(RpcProtocolError) as rejected:
            _quantity(response["blockNumber"])
        self.assertIn('response["blockNumber"]', rejected.exception.__notes__[0])

        chain_document = {"chain_id": True}
        with self.assertRaises(ConfigurationError) as rejected:
            catalog_quantity(chain_document["chain_id"])
        self.assertIn('chain_document["chain_id"]', rejected.exception.__notes__[0])

    def test_no_source_code_values_in_public_message_or_notes(self):
        private_value = "not a hex string: bearer-token-placeholder"
        with self.assertRaises(RpcProtocolError) as rejected:
            _bytes(private_value, 32)

        self.assertNotIn(private_value, str(rejected.exception))
        self.assertNotIn(private_value, "\n".join(rejected.exception.__notes__))
        self.assertIn("private_value", rejected.exception.__notes__[0])

    def test_source_unavailable_does_not_affect_original_error(self):
        namespace = {"_bytes": _bytes}
        exec(
            compile(
                "def invoke():\n    return _bytes(None, 32)\n",
                "<source-not-available>",
                "exec",
            ),
            namespace,
        )
        with self.assertRaises(RpcProtocolError) as rejected:
            namespace["invoke"]()

        self.assertFalse(getattr(rejected.exception, "__notes__", None))
