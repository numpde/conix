"""T1-A selected-read retry must traverse the real installed Tier-0 journal.

This contained test uses the ordinary disposable Anvil/PostgreSQL test stack.
It injects ONE transient failure before an actual selected-log read, delegates
the retry through the concrete RPC, and witnesses durable block publication.
"""

import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
from psycopg import AsyncConnection

from anvil_catalog import catalog_for_anvil
from conix.core import Writer
from conix.journal import Journal
from conix.rpc import RPC, RpcUnavailable
from conix.resilience.rpc_retry import run_with_read_retries


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires owner-initialized disposable PostgreSQL and real Anvil",
)
class TierOneConcreteDelegation(unittest.IsolatedAsyncioTestCase):
    async def test_retried_selected_read_reaches_real_journal_publication(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]

        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async def fixture_rpc(method, params):
                response = await client.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                response.raise_for_status()
                document = response.json()
                self.assertNotIn("error", document)
                return document["result"]

            genesis = await fixture_rpc("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            await fixture_rpc("evm_mine", [])
            head = int((await fixture_rpc("eth_getBlockByNumber", ["latest", False]))["number"], 16)

        dropped = []
        advanced = []
        published = []
        original_rpc = RPC.call
        original_advance = Writer.advance_one
        original_publish = Journal.publish_block

        async def transient_once(rpc, method, params, *, deadline):
            if method == "eth_getLogs" and not dropped:
                dropped.append((method, params, deadline))
                raise RpcUnavailable("selected read temporarily unavailable") from httpx.ConnectError(
                    "fixture read interrupted",
                )
            return await original_rpc(rpc, method, params, deadline=deadline)

        async def real_advance(writer):
            result = await original_advance(writer)
            advanced.append(result)
            return result

        async def real_publish(journal, *, block, events):
            result = await original_publish(journal, block=block, events=events)
            published.append(block.number)
            return result

        async def checkpoint():
            async with await AsyncConnection.connect(reader_dsn, connect_timeout=5) as database:
                return await (await database.execute(
                    "SELECT last_block FROM conix.source_progress WHERE singleton=1",
                )).fetchone()

        stop = asyncio.Event()
        with (
            patch.object(RPC, "call", transient_once),
            patch.object(Writer, "advance_one", real_advance),
            patch.object(Journal, "publish_block", real_publish),
        ):
            task = asyncio.create_task(run_with_read_retries(
                catalog, dsn, stop, poll_interval=0.05,
            ))
            try:
                async with asyncio.timeout(20):
                    while True:
                        if task.done():
                            await task
                            self.fail("The Tier-1 composition exited before publishing the selected block.")
                        row = await checkpoint()
                        if row[0] is not None and row[0] >= head:
                            break
                        await asyncio.sleep(0.05)
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=10)

        self.assertEqual(len(dropped), 1)
        self.assertGreaterEqual(len(advanced), 1)
        self.assertIn(head, published)
        self.assertGreaterEqual((await checkpoint())[0], head)
        self.assertTrue(all(result is None or result.last_block.number <= head for result in advanced))


if __name__ == "__main__":
    unittest.main()
