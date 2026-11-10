"""Real disposable PostgreSQL role boundary before generic journal installation.

No old collections tables, source_progress schema or PayToPlay observer is
created. The owner-only generic DDL belongs to T0-2, not this test lane.
"""
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
            self.assertEqual(row[0], 0, "Prototype tables were installed unexpectedly.")
            with self.assertRaises(InsufficientPrivilege):
                await connection.execute("CREATE TABLE conix.unapproved (id integer)")

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

    async def test_neither_role_can_read_other_application_schema(self):
        for credential in ("CONIX_TEST_DSN", "CONIX_TEST_READ_DSN"):
            with self.subTest(credential=credential):
                async with await AsyncConnection.connect(
                    os.environ[credential], connect_timeout=5,
                ) as connection:
                    with self.assertRaises(InsufficientPrivilege):
                        await connection.execute("SELECT * FROM ledger.operator_private")
