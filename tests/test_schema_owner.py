"""One-shot owner-only installation in the disposable PostgreSQL project."""

import os
from pathlib import Path
import unittest

import httpx

from anvil_catalog import catalog_for_anvil
from conix.catalog import prepare_catalog
from conix.schema import SchemaError, initialize


@unittest.skipUnless(os.getenv("CONIX_TEST_OWNER_DSN"), "Runs only in the owner initializer")
class SchemaOwnerTests(unittest.IsolatedAsyncioTestCase):

    async def test_install_fresh_history_and_refuse_second_installation(self):
        catalog = prepare_catalog(Path("/tests/fixtures/catalog.toml"))
        await initialize(os.environ["CONIX_TEST_OWNER_DSN"], catalog)

        with self.assertRaises(SchemaError):
            await initialize(os.environ["CONIX_TEST_OWNER_DSN"], catalog)


    @unittest.skipUnless(
        os.getenv("CONIX_TEST_ANVIL_OWNER_DSN") and os.getenv("CONIX_TEST_ANVIL_URL"),
        "Requires the separate owner-only Anvil history database",
    )
    async def test_install_anvil_source_into_isolated_database(self):
        """Owner observes genesis, then installs the same offline source."""
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            result = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getBlockByNumber",
                    "params": ["0x0", False],
                },
            )
            result.raise_for_status()
            genesis = result.json()["result"]

        catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
        await initialize(os.environ["CONIX_TEST_ANVIL_OWNER_DSN"], catalog)
