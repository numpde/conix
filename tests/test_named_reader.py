"""The named Reader role uses the original installed Reader and no writer authority."""

import asyncio
from dataclasses import replace
from io import StringIO
import json
import os
import sys
import unittest
from unittest.mock import patch

import httpx

from anvil_catalog import catalog_for_anvil
from conix.journal import Journal, StorageUnavailable
from conix.reader import HistoryUnavailable
from conix.schema import SchemaError, initialize
from conix import reader_cli


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_READ_DSN")
    and os.getenv("CONIX_TEST_ANVIL_DSN"),
    "Requires the owner-initialized disposable Anvil and PostgreSQL source",
)
class NamedReaderAnvilContracts(unittest.IsolatedAsyncioTestCase):
    async def test_selected_reader_role_observes_the_real_source_without_writer_access(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            result = await http.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                "params": ["0x0", False],
            })
            result.raise_for_status()
            genesis = result.json()["result"]
        writer_source = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
        reader_source = replace(
            writer_source,
            database=replace(writer_source.database, runtime_role="conix_reader"),
        )

        result = await reader_cli.inspect(
            reader_source, os.environ["CONIX_TEST_ANVIL_READ_DSN"],
        )
        self.assertEqual(result["outcome"], "reader_ready")
        self.assertEqual(result["source_id"], writer_source.source_id.hex())
        self.assertEqual(
            set(result), {"outcome", "source_id", "checkpoint", "generation"},
        )
        if result["checkpoint"] is not None:
            self.assertIsInstance(result["checkpoint"], int)
            self.assertIsInstance(result["generation"], str)

        with self.assertRaises(HistoryUnavailable):
            await reader_cli.inspect(
                reader_source, os.environ["CONIX_TEST_ANVIL_DSN"],
            )
        with self.assertRaises(StorageUnavailable):
            await Journal.open(
                catalog=reader_source,
                database_dsn=os.environ["CONIX_TEST_ANVIL_READ_DSN"],
            )
        with self.assertRaises(SchemaError):
            await initialize(
                "must-not-connect:owner-secret", reader_source,
            )


class ReaderCliDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_reader_cli_never_prints_exception_message_or_selected_dsn(self):
        private = "postgresql://conix_reader:never-echo-this@postgres/test"

        with (
            patch.object(reader_cli.sys, "argv", ["reader_cli", "check"]),
            patch.object(reader_cli, "load_runtime_inputs",
                         return_value=(object(), private)),
            patch.object(reader_cli, "inspect",
                         side_effect=RuntimeError(private)),
            patch("sys.stderr", new_callable=StringIO) as errors,
            patch("sys.stdout", new_callable=StringIO) as output,
        ):
            with self.assertRaises(SystemExit) as ended:
                reader_cli.main()
        self.assertEqual(ended.exception.code, 2)
        self.assertIn("CONIX-FAILURE-V1", errors.getvalue())
        self.assertIn('"phase":"reader_check"', errors.getvalue())
        self.assertNotIn("never-echo-this", errors.getvalue())
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
