"""Real standalone service: existing Anvil/PG writer, tip and later block.

This suite is separate from orchestration mocks. It reuses the installed
generic source initialized by the existing owner-only test service.
"""

import asyncio
import inspect
import os
from pathlib import Path
import tempfile
import unittest

import httpx
from psycopg import AsyncConnection

from anvil_catalog import catalog_for_anvil
from conix import app
from conix.history_cli import inspect_history
from conix.journal import WRITER_LOCK


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN"),
    "Requires disposable Anvil/PostgreSQL real-chain stack",
)
class StandaloneAnvilContracts(unittest.IsolatedAsyncioTestCase):
    async def test_catchup_tip_lock_release_later_block_and_inspection(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def rpc(method, params):
                reply = await http.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                reply.raise_for_status()
                value = reply.json()
                self.assertNotIn("error", value)
                return value["result"]

            genesis = await rpc("eth_getBlockByNumber", ["0x0", False])
            source = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            head = int((await rpc("eth_getBlockByNumber", ["latest", False]))["number"], 16)

            async def checkpoint():
                async with await AsyncConnection.connect(dsn, connect_timeout=5) as connection:
                    row = await (await connection.execute(
                        "SELECT last_block FROM conix.source_progress WHERE singleton=1",
                    )).fetchone()
                    return row[0]

            async def reach(target):
                for _ in range(100):
                    if (last := await checkpoint()) is not None and last >= target:
                        return
                    await asyncio.sleep(0.05)
                self.fail(f"Standalone indexer did not publish through block {target}.")

            stop = asyncio.Event()
            with tempfile.TemporaryDirectory() as folder:
                kwargs = {"poll_interval": 0.3}
                if "socket_path" in inspect.signature(app.run).parameters:
                    kwargs["socket_path"] = Path(folder) / "control.sock"
                running = asyncio.create_task(app.run(source, dsn, stop, **kwargs))
                try:
                    await reach(head)
                    # At a checked tip, the writer must relinquish advisory
                    # ownership rather than retain an idle long-lived lock.
                    released = False
                    for _ in range(60):
                        async with await AsyncConnection.connect(dsn, connect_timeout=5) as connection:
                            acquired = await (await connection.execute(
                                "SELECT pg_try_advisory_lock(%s)", (WRITER_LOCK,),
                            )).fetchone()
                            if acquired == (True,):
                                await connection.execute(
                                    "SELECT pg_advisory_unlock(%s)", (WRITER_LOCK,),
                                )
                                released = True
                                break
                        await asyncio.sleep(0.05)
                    self.assertTrue(released, "Idle indexer retained exclusive writer ownership.")

                    await rpc("evm_mine", [])
                    await reach(head + 1)
                    result = await inspect_history(source, dsn, ancestor=head + 1)
                    self.assertEqual(result["blocks"][0]["number"], head + 1)
                finally:
                    stop.set()
                    await asyncio.wait_for(running, timeout=10)
