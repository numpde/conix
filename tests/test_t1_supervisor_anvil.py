"""T1-B executable witness: a new ordinary session after real RPC exhaustion.

Use existing owner-initialized PostgreSQL and Anvil, the SAME Tier-0 app.run
and the original source/journal. Test instrumentation delegates to real
methods; it never mocks successful publication or fabricates progress.
"""

import asyncio
from contextlib import asynccontextmanager
import os
import unittest
from unittest.mock import patch

import httpx
from psycopg import AsyncConnection

from anvil_catalog import catalog_for_anvil
from conix import app
from conix.core import open_writer
from conix.journal import Journal
from conix.rpc import RPC, RpcUnavailable
from conix.resilience import rpc_retry, supervisor


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL")
    and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires the owner-initialized disposable Anvil and PostgreSQL",
)
class SupervisedRealSessionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_reacquires_writer_and_retained_history(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        database_dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]

        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def call(method, params):
                response = await http.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                response.raise_for_status()
                result = response.json()
                self.assertNotIn("error", result)
                return result["result"]

            genesis = await call("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            await call("evm_mine", [])
            target = int((await call("eth_getBlockByNumber", ["latest", False]))["number"], 16)

        async def checkpoint():
            async with await AsyncConnection.connect(reader_dsn, connect_timeout=5) as sql:
                return await (await sql.execute(
                    "SELECT last_block,revision,generation,source_id "
                    "FROM conix.source_progress WHERE singleton=1"
                )).fetchone()

        before = await checkpoint()
        self.assertEqual(before[3], catalog.source_id)

        original_http_read = RPC.call
        original_writer = open_writer
        original_app = app.run
        original_publication = Journal.publish_block
        dropped = []
        sessions = []
        retired = []
        actual_applications = []
        actual_publications = []
        active_writers = 0

        async def interrupted_once(rpc, method, params, *, deadline):
            if method == "eth_getLogs" and len(dropped) < 3:
                dropped.append((method, params, deadline))
                raise RpcUnavailable("Selected read connection failed.") from httpx.ReadError(
                    "disposable RPC link disconnected"
                )
            return await original_http_read(rpc, method, params, deadline=deadline)

        @asynccontextmanager
        async def observed_writer(**kwargs):
            nonlocal active_writers
            selected = None
            try:
                async with original_writer(**kwargs) as writer:
                    self.assertEqual(active_writers, 0, "Restart admitted before prior retirement.")
                    self.assertIs(writer.catalog, catalog)
                    self.assertIs(writer.journal.catalog, catalog)
                    active_writers += 1
                    selected = writer
                    sessions.append(writer)
                    yield writer
            finally:
                if selected is not None:
                    self.assertTrue(selected.journal.connection.closed,
                                    "Application returned before the SQL writer retired.")
                    active_writers -= 1
                    retired.append(selected)

        async def real_application(*args, **kwargs):
            actual_applications.append(args)
            return await original_app(*args, **kwargs)

        async def real_publication(journal, *, block, events):
            result = await original_publication(journal, block=block, events=events)
            actual_publications.append(block.number)
            return result

        stop = asyncio.Event()
        with (
            patch.object(RPC, "call", interrupted_once),
            patch.object(rpc_retry, "open_writer", observed_writer),
            patch.object(rpc_retry.app, "run", real_application),
            patch.object(Journal, "publish_block", real_publication),
        ):
            task = asyncio.create_task(supervisor.supervise(catalog, database_dsn, stop))
            try:
                async with asyncio.timeout(25):
                    while True:
                        if task.done():
                            await task
                            self.fail("Supervision exited before reaching the real source head.")
                        current = await checkpoint()
                        if current[0] is not None and current[0] >= target:
                            break
                        await asyncio.sleep(0.05)
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=10)

        final = await checkpoint()
        self.assertEqual(len(dropped), 3, "T1-A did not exhaust its bounded read attempts.")
        self.assertEqual(len(sessions), 2, "T1-B did not open exactly one fresh session.")
        self.assertEqual(retired, sessions)
        self.assertEqual(active_writers, 0)
        self.assertEqual(len(actual_applications), 2)
        self.assertIn(target, actual_publications)
        self.assertEqual(final[3], catalog.source_id)
        self.assertGreaterEqual(final[0], target)
        if before[0] is not None:
            self.assertGreaterEqual(final[1], before[1])
            self.assertEqual(final[2], before[2])


if __name__ == "__main__":
    unittest.main()
