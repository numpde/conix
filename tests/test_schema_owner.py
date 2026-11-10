"""One-shot owner-only installation in the disposable PostgreSQL project."""

import os
from pathlib import Path
import unittest

from conix.catalog import prepare_catalog
from conix.schema import SchemaError, initialize


@unittest.skipUnless(os.getenv("CONIX_TEST_OWNER_DSN"), "Runs only in the owner initializer")
class SchemaOwnerTests(unittest.IsolatedAsyncioTestCase):

    async def test_install_fresh_history_and_refuse_second_installation(self):
        catalog = prepare_catalog(Path("/tests/fixtures/catalog.toml"))
        await initialize(os.environ["CONIX_TEST_OWNER_DSN"], catalog)

        with self.assertRaises(SchemaError):
            await initialize(os.environ["CONIX_TEST_OWNER_DSN"], catalog)
