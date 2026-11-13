"""T0-7e: Faults against the installed continuous process, not a mock writer.

Use the existing disposable PostgreSQL/Anvil stack and unmodified Tier-0
scanner/journal/reader. A loopback, test-owned RPC relay can return a selected
bad observation or hold a read while the actual process is terminated.
"""

import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import sys
import unittest

import httpx
from psycopg import AsyncConnection

from anvil_catalog import FIXTURES, source_toml_for_anvil
from pg_commit_reply import without_commit_reply


class FaultRelay:
    """Pass through genuine Anvil JSON-RPC except one selected failure."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.mode = "pass"
        self.target_height = None
        self.entered = asyncio.Event()
        self.verified = asyncio.Event()
        self.release = asyncio.Event()
        self.server = None
        self.client = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(trust_env=False, timeout=5)
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *_args):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        await self.client.aclose()

    async def handle(self, reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            if len(header) > 8192:
                return
            sizes = [
                int(line.split(b":", 1)[1].strip())
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ]
            if len(sizes) != 1 or not 0 < sizes[0] <= 65536:
                return
            message = json.loads(await reader.readexactly(sizes[0]))
            method = message["method"]
            params = message["params"]
            if method == "eth_chainId":
                # The installed CLI registers its signal handlers before
                # opening the writer and verifying the selected chain.
                self.verified.set()
            status = 200

            if self.mode == "offline":
                status, payload = 503, b""
            elif self.mode == "wrong_chain" and method == "eth_chainId":
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": message["id"], "result": "0x2",
                }).encode()
            elif (self.mode == "missing_header"
                  and method == "eth_getBlockByNumber"
                  and params[0] == hex(self.target_height)):
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": message["id"], "result": None,
                }).encode()
            elif self.mode == "bad_logs" and method == "eth_getLogs":
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": message["id"],
                    "result": {"invalid": "not a log array"},
                }).encode()
            else:
                if self.mode == "hold_logs" and method == "eth_getLogs":
                    self.entered.set()
                    await self.release.wait()
                response = await self.client.post(self.upstream, json=message)
                status, payload = response.status_code, response.content

            phrase = b"OK" if status == 200 else b"Service Unavailable"
            wire = (
                b"HTTP/1.1 " + str(status).encode() + b" " + phrase
                + b"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(payload)).encode() + b"\r\n\r\n" + payload
            )
            writer.write(wire)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, KeyError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL")
    and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires the existing initialized disposable Anvil/PostgreSQL stack",
)
class InstalledTier07FaultAcceptance(unittest.IsolatedAsyncioTestCase):
    """Verify actual process failure and durable recovery without Tier-1 policy."""

    async def asyncSetUp(self):
        self.url = os.environ["CONIX_TEST_ANVIL_URL"]
        self.dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        self.reader_dsn = os.environ["CONIX_TEST_ANVIL_READ_DSN"]
        self.http = httpx.AsyncClient(trust_env=False, timeout=5)
        self.relay = await FaultRelay(self.url).__aenter__()
        self.processes = []

        genesis = await self.rpc("eth_getBlockByNumber", ["0x0", False])
        self.genesis_hash = genesis["hash"]
        self.target = Path("/run/conix")
        self.assertEqual(
            (self.target.stat().st_uid, stat.S_IMODE(self.target.stat().st_mode)),
            (65532, 0o700),
        )
        self.assertEqual(list(self.target.iterdir()), [])
        (self.target / "abi").mkdir(mode=0o700)

        for filename in ("erc20.json", "indexed_scalars.json"):
            destination = self.target / "abi" / filename
            destination.write_bytes((FIXTURES / "abi" / filename).read_bytes())
            destination.chmod(0o600)

        config = source_toml_for_anvil(genesis_hash=self.genesis_hash, rpc_url=self.relay.url)
        (self.target / "config.toml").write_text(config)
        (self.target / "connection.dsn").write_text(self.dsn + "\n")
        for filename in ("config.toml", "connection.dsn"):
            (self.target / filename).chmod(0o600)

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.communicate(), 10)
        await self.relay.__aexit__(None, None, None)
        await self.http.aclose()
        for file in (self.target / "abi").iterdir():
            file.unlink()
        (self.target / "abi").rmdir()
        (self.target / "config.toml").unlink()
        (self.target / "connection.dsn").unlink()

    async def rpc(self, method, params):
        response = await self.http.post(
            self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        result = response.json()
        self.assertNotIn("error", result)
        return result["result"]

    async def progress(self):
        async with await AsyncConnection.connect(self.reader_dsn, connect_timeout=5) as db:
            return await (await db.execute(
                "SELECT last_block,revision,generation::text,blocked_reason "
                "FROM conix.source_progress WHERE singleton=1",
            )).fetchone()

    async def event_count(self, transaction):
        async with await AsyncConnection.connect(self.reader_dsn, connect_timeout=5) as db:
            return (await (await db.execute(
                "SELECT count(*) FROM conix.events WHERE transaction_hash=%s",
                (bytes.fromhex(transaction[2:]),),
            )).fetchone())[0]

    async def start(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "conix", *(arguments or ("run",)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)
        return process

    async def failure(self, process, *, cause):
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=12)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(cause.encode(), stderr)
        self.assertNotIn(self.dsn.encode(), stdout + stderr)
        return stdout

    async def stop(self, process):
        process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        self.assertEqual(process.returncode, 0, stderr[-500:].decode(errors="replace"))
        self.assertIn(b'"outcome": "stopped"', stdout)
        self.assertNotIn(self.dsn.encode(), stdout + stderr)

    async def reach(self, process, height):
        async with asyncio.timeout(20):
            while True:
                self.assertIsNone(process.returncode, "Installed service exited before reaching its head.")
                state = await self.progress()
                if state[0] is not None and state[0] >= height:
                    return state
                await asyncio.sleep(0.05)

    async def baseline(self):
        head = int((await self.rpc("eth_getBlockByNumber", ["latest", False]))["number"], 16)
        process = await self.start()
        await asyncio.wait_for(self.relay.verified.wait(), timeout=10)
        await self.reach(process, head)
        await self.stop(process)
        return head, await self.progress()

    async def emit_transfer(self):
        """One real selected Transfer; no duplicate submission after uncertainty."""
        token = "0x" + "11" * 20
        topic = bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
        from_topic = bytes.fromhex("00" * 12 + "22" * 20)
        to_topic = bytes.fromhex("00" * 12 + "33" * 20)
        runtime = (b"\x60\x07\x60\x00\x52" + b"\x7f" + to_topic
                   + b"\x7f" + from_topic + b"\x7f" + topic
                   + b"\x60\x20\x60\x00\xa3\x00")
        await self.rpc("anvil_setCode", [token, "0x" + runtime.hex()])
        account = (await self.rpc("eth_accounts", []))[0]
        transaction = await self.rpc("eth_sendTransaction", [{
            "from": account, "to": token, "gas": "0x100000",
        }])
        async with asyncio.timeout(5):
            while (receipt := await self.rpc("eth_getTransactionReceipt", [transaction])) is None:
                await asyncio.sleep(0.05)
        self.assertEqual(receipt["status"], "0x1")
        self.assertEqual(len(receipt["logs"]), 1)
        return transaction, int(receipt["blockNumber"], 16)

    async def test_wrong_source_missing_header_and_bad_logs_fail_before_publication(self):
        _, initial = await self.baseline()

        self.relay.mode = "wrong_chain"
        failed = await self.start()
        await self.failure(failed, cause="BranchConflict")
        self.assertEqual(await self.progress(), initial)

        transaction, height = await self.emit_transfer()
        self.assertEqual(await self.event_count(transaction), 0)

        self.relay.mode, self.relay.target_height = "missing_header", height
        failed = await self.start()
        await self.failure(failed, cause="RpcUnavailable")
        self.assertEqual(await self.progress(), initial)
        self.assertEqual(await self.event_count(transaction), 0)

        self.relay.mode = "bad_logs"
        failed = await self.start()
        await self.failure(failed, cause="RpcProtocolError")
        self.assertEqual(await self.progress(), initial)
        self.assertEqual(await self.event_count(transaction), 0)

        # Explicit restart, not an automatic retry of a rejected observation.
        self.relay.mode = "pass"
        healthy = await self.start()
        await self.reach(healthy, height)
        await self.stop(healthy)
        self.assertEqual(await self.event_count(transaction), 1)

    async def test_kill_during_log_acquisition_preserves_prefix(self):
        _, initial = await self.baseline()
        transaction, height = await self.emit_transfer()
        self.relay.mode = "hold_logs"

        active = await self.start()
        await asyncio.wait_for(self.relay.entered.wait(), timeout=10)
        active.kill()
        await asyncio.wait_for(active.communicate(), timeout=10)
        self.assertEqual(active.returncode, -signal.SIGKILL)
        self.relay.release.set()
        self.relay.mode = "pass"

        self.assertEqual(await self.progress(), initial)
        self.assertEqual(await self.event_count(transaction), 0)

        resumed = await self.start()
        await self.reach(resumed, height)
        await self.stop(resumed)
        self.assertEqual(await self.event_count(transaction), 1)
        self.assertIsNone((await self.progress())[3])

    async def test_lost_writer_connection_during_evidence_acquisition(self):
        _, initial = await self.baseline()
        transaction, height = await self.emit_transfer()
        self.relay.mode = "hold_logs"

        active = await self.start()
        await asyncio.wait_for(self.relay.entered.wait(), timeout=10)

        # Same PostgreSQL login may terminate its own backend. The paused
        # RPC request ensures the real writer session is still open.
        async with await AsyncConnection.connect(self.dsn, connect_timeout=5) as observer:
            rows = await (await observer.execute(
                "SELECT pid FROM pg_stat_activity "
                "WHERE usename=current_user AND datname=current_database() "
                "AND pid <> pg_backend_pid()",
            )).fetchall()
            self.assertEqual(len(rows), 1, "Expected exactly the active Conix writer backend.")
            killed = await (await observer.execute(
                "SELECT pg_terminate_backend(%s)", (rows[0][0],),
            )).fetchone()
            self.assertEqual(killed, (True,))

        self.relay.release.set()
        self.relay.mode = "pass"
        stdout, stderr = await asyncio.wait_for(active.communicate(), timeout=12)
        self.assertNotEqual(active.returncode, 0)
        self.assertNotIn(self.dsn.encode(), stdout + stderr)
        self.assertEqual(await self.progress(), initial)
        self.assertEqual(await self.event_count(transaction), 0)

        recovered = await self.start()
        await self.reach(recovered, height)
        await self.stop(recovered)
        self.assertEqual(await self.event_count(transaction), 1)

    async def test_uncertain_postgresql_commit_reconciles_on_explicit_restart(self):
        """Lose a real COMMIT reply while the installed CLI publishes one block."""
        _, initial = await self.baseline()
        transaction, height = await self.emit_transfer()
        credential_file = self.target / "connection.dsn"

        try:
            async with asyncio.timeout(20), without_commit_reply(self.dsn) as (relay_dsn, dropped):
                credential_file.write_text(relay_dsn + "\n")
                credential_file.chmod(0o600)

                active = await self.start()
                stdout, stderr = await asyncio.wait_for(active.communicate(), timeout=12)
                self.assertNotEqual(active.returncode, 0)
                self.assertIn(b"COMMIT", stderr)
                self.assertNotIn(self.dsn.encode(), stdout + stderr)
                self.assertNotIn(b'"block_number": ' + str(height).encode(), stdout)
                await dropped
        finally:
            credential_file.write_text(self.dsn + "\n")
            credential_file.chmod(0o600)

        # Lost response does not establish an uncommitted transaction. Read
        # actual PostgreSQL before making another write attempt.
        observed = await self.progress()
        self.assertIn(observed[0], (initial[0], height))
        self.assertEqual(
            await self.event_count(transaction),
            0 if observed[0] == initial[0] else 1,
        )
        self.assertIsNone(observed[3])

        # No replay-specific code path: a NEW ordinary application instance
        # must consult the persisted checkpoint and finish without duplicates.
        self.relay.verified.clear()
        restarted = await self.start()
        await asyncio.wait_for(self.relay.verified.wait(), timeout=10)
        await self.reach(restarted, height)
        await self.stop(restarted)
        self.assertEqual(await self.event_count(transaction), 1)

    async def test_detect_reorganization_then_explicitly_repair_and_resume(self):
        base, _ = await self.baseline()
        snapshot = await self.rpc("evm_snapshot", [])
        orphan, height = await self.emit_transfer()
        self.assertEqual(height, base + 1)

        indexed = await self.start()
        published = await self.reach(indexed, height)
        await self.stop(indexed)
        self.assertEqual(await self.event_count(orphan), 1)

        self.assertTrue(await self.rpc("evm_revert", [snapshot]))
        await self.rpc("evm_mine", [])
        replacement = await self.rpc("eth_getBlockByNumber", [hex(height), False])
        self.assertNotEqual(replacement["hash"], "0x" + "0" * 64)

        stale = await self.start()
        await self.failure(stale, cause="BranchConflict")
        self.assertEqual(await self.progress(), published)

        inspected = await self.start("history", "inspect", "--ancestor", str(base))
        output, stderr = await asyncio.wait_for(inspected.communicate(), timeout=10)
        self.assertEqual(inspected.returncode, 0, stderr.decode(errors="replace"))
        self.assertIn(b'"number": ' + str(base).encode(), output)

        repaired = await self.start("history", "repair", "--ancestor", str(base))
        output, stderr = await asyncio.wait_for(repaired.communicate(), timeout=10)
        self.assertEqual(repaired.returncode, 0, stderr.decode(errors="replace"))
        self.assertIn(b'"outcome": "repaired"', output)
        self.assertEqual((await self.progress())[0], base)
        self.assertNotEqual((await self.progress())[2], published[2])
        self.assertEqual(await self.event_count(orphan), 0)

        restarted = await self.start()
        after = await self.reach(restarted, height)
        await self.stop(restarted)
        self.assertEqual(after[0], height)
        self.assertEqual(await self.event_count(orphan), 0)
        self.assertIsNone(after[3])


if __name__ == "__main__":
    unittest.main()
