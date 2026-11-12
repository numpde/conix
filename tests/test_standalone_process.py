"""The installed standalone CLI runs as a separate real Unix process.

The contained test supplies private fixture inputs only through a disposable
rootless UID-owned tmpfs; no new production staging or privilege path is added.
"""

import asyncio
import os
from pathlib import Path
import signal
import stat
import sys
import unittest

import httpx
from psycopg import AsyncConnection

from anvil_catalog import FIXTURES, source_toml_for_anvil
from conix.history_cli import inspect_history
from anvil_catalog import catalog_for_anvil


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN"),
    "Requires initialized disposable Anvil/PostgreSQL and contained runtime inputs",
)
class InstalledStandaloneProcessContracts(unittest.IsolatedAsyncioTestCase):

    async def test_installed_cli_catches_up_follows_tip_and_stops_on_sigterm(self):
        target = Path("/run/conix")
        self.assertTrue(target.is_dir(), "Test runner must provide isolated runtime tmpfs.")
        self.assertEqual((target.stat().st_uid, stat.S_IMODE(target.stat().st_mode)), (65532, 0o700))
        self.assertEqual(tuple(target.iterdir()), ())

        url, dsn = os.environ["CONIX_TEST_ANVIL_URL"], os.environ["CONIX_TEST_ANVIL_DSN"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async def rpc(method, args):
                response = await client.post(
                    url, json={"jsonrpc": "2.0", "id": 1,
                               "method": method, "params": args},
                )
                response.raise_for_status()
                document = response.json()
                self.assertNotIn("error", document)
                return document["result"]

            genesis = await rpc("eth_getBlockByNumber", ["0x0", False])
            source = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            declared = source_toml_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)
            (target / "abi").mkdir(mode=0o700)
            for filename in ("erc20.json", "indexed_scalars.json"):
                destination = target / "abi" / filename
                destination.write_bytes((FIXTURES / "abi" / filename).read_bytes())
                destination.chmod(0o600)
            (target / "config.toml").write_text(declared, encoding="utf-8")
            (target / "connection.dsn").write_text(dsn + "\n", encoding="utf-8")
            for filename in ("config.toml", "connection.dsn"):
                (target / filename).chmod(0o600)

            async def checkpoint():
                async with await AsyncConnection.connect(dsn, connect_timeout=5) as db:
                    return (await (await db.execute(
                        "SELECT last_block FROM conix.source_progress WHERE singleton = 1"
                    )).fetchone())[0]

            async def await_block(height, process):
                async with asyncio.timeout(15):
                    while True:
                        self.assertIsNone(process.returncode, "Standalone CLI exited before reaching its checkpoint.")
                        current = await checkpoint()
                        if current is not None and current >= height:
                            return
                        await asyncio.sleep(0.05)

            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "conix", "run",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.sleep(0.25)
                self.assertIsNone(process.returncode, "Standalone CLI failed to start.")
                head = await rpc("eth_getBlockByNumber", ["latest", False])
                height = int(head["number"], 16)
                await await_block(height, process)
                await rpc("evm_mine", [])
                await await_block(height + 1, process)

                # Only a process-level SIGTERM proves the installed CLI's
                # signal handler and its writer/HTTP resource retirement.
                process.send_signal(signal.SIGTERM)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
                self.assertEqual(process.returncode, 0, stderr[-1024:].decode("utf-8", errors="replace"))
                self.assertIn(b'"outcome": "stopped"', stdout)
                self.assertNotIn(dsn.encode("utf-8"), stdout + stderr)

                result = await inspect_history(source, dsn, ancestor=height + 1)
                self.assertEqual(result["blocks"][0]["number"], height + 1)
            finally:
                if process.returncode is None:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=10)
                for member in (target / "abi").iterdir():
                    member.unlink()
                (target / "abi").rmdir()
                for member in ("config.toml", "connection.dsn"):
                    (target / member).unlink()
