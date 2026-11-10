"""Check the freshly installed generic journal's runtime and reader privileges."""
import os
import unittest

from psycopg import AsyncConnection
from psycopg.errors import InsufficientPrivilege


@unittest.skipUnless(os.getenv("CONIX_TEST_DSN") and os.getenv("CONIX_TEST_READ_DSN"),
                     "Requires the container-owned disposable PostgreSQL stack")
class PostgreSQLBoundaryTests(unittest.IsolatedAsyncioTestCase):

    async def test_runtime_cannot_own_or_create_schema(self):
        async with await AsyncConnection.connect(
            os.environ["CONIX_TEST_DSN"], connect_timeout=5,
        ) as connection:
            row = await (await connection.execute(
                "SELECT current_user, pg_get_userbyid(nspowner), "
                "has_schema_privilege(current_user,'conix','CREATE') "
                "FROM pg_namespace WHERE nspname='conix'"
            )).fetchone()
            self.assertEqual(row, ("conix_runtime", "conix_owner", False))
            row = await (await connection.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='conix'"
            )).fetchone()
            self.assertEqual(row[0], 3, "Owner did not install the three generic journal tables.")
            with self.assertRaises(InsufficientPrivilege):
                await connection.execute("CREATE TABLE conix.unapproved (id integer)")

    async def test_installed_source_identity_and_raw_log_columns(self):
        from pathlib import Path

        from conix.catalog import prepare_catalog

        catalog = prepare_catalog(Path("/tests/fixtures/catalog.toml"))
        async with await AsyncConnection.connect(
            os.environ["CONIX_TEST_DSN"],
            connect_timeout=5,
        ) as connection:
            row = await (await connection.execute(
                "SELECT source_id, chain_id, genesis_hash, history_start, "
                "last_block, revision FROM conix.source_progress WHERE singleton = 1"
            )).fetchone()
            self.assertEqual(row, (
                catalog.source_id,
                catalog.chain.chain_id,
                catalog.chain.genesis_hash,
                catalog.chain.start_block,
                None,
                0,
            ))
            columns = await (await connection.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'conix' AND table_name = 'events'"
            )).fetchall()
            self.assertIn(("data", "bytea"), columns)
            self.assertIn(("topic3", "bytea"), columns)

    async def test_reader_has_distinct_identity_and_no_mutation_access(self):
        async with await AsyncConnection.connect(
            os.environ["CONIX_TEST_READ_DSN"], connect_timeout=5,
        ) as connection:
            row = await (await connection.execute(
                "SELECT current_user, has_schema_privilege(current_user,'conix','CREATE'), "
                "has_schema_privilege(current_user,'conix','USAGE')"
            )).fetchone()
            self.assertEqual(row, ("conix_reader", False, True))
            with self.assertRaises(InsufficientPrivilege):
                await connection.execute("CREATE TABLE conix.unapproved (id integer)")
            await connection.rollback()
            with self.assertRaises(InsufficientPrivilege):
                await connection.execute(
                    "UPDATE conix.source_progress SET revision = revision + 1 WHERE singleton = 1"
                )

    async def test_neither_role_can_read_other_application_schema(self):
        for credential in ("CONIX_TEST_DSN", "CONIX_TEST_READ_DSN"):
            with self.subTest(credential=credential):
                async with await AsyncConnection.connect(
                    os.environ[credential], connect_timeout=5,
                ) as connection:
                    with self.assertRaises(InsufficientPrivilege):
                        await connection.execute("SELECT * FROM ledger.operator_private")
