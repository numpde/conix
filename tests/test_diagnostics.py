"""Developer source-expression notes without argument labels or value leaks."""

import unittest

from conix.catalog import ConfigurationError, _quantity as catalog_quantity
from conix.core import _bytes, _quantity
from conix.rpc import RpcProtocolError


class ExpressionDiagnostics(unittest.TestCase):

    def test_two_calls_on_one_line_have_distinct_argument_notes(self):
        for field in ("blockHash", "address"):
            response = {
                "blockHash": "0x" + "11" * 32,
                "address": "0x" + "22" * 20,
            }
            response[field] = None

            with self.subTest(field=field), self.assertRaises(RpcProtocolError) as rejected:
                _bytes(response.get("blockHash"), 32); _bytes(response.get("address"), 20)

            self.assertEqual(
                rejected.exception.__notes__,
                [f'argument expression: response.get("{field}")'],
            )

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
