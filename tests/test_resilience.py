"""Follow the chain tip without taking over Tier-0 publication or recovery."""

import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from abi_golden_vectors import ERC20, TO, TRANSFER_SIGNATURE
from anvil_catalog import catalog_for_anvil
from conix.core import Writer
from conix.journal import Journal
from conix.reader import open_reader
from conix.resilience.supervisor import follow
from conix.worker import advance


class ContinuousAdvanceContracts(unittest.IsolatedAsyncioTestCase):
    async def test_catches_up_without_waiting_between_published_blocks(self):
        stop = asyncio.Event()
        calls = []

        async def advanced(catalog, database_dsn):
            calls.append((catalog, database_dsn))
            if len(calls) == 3:
                stop.set()
                return None
            return object()

        with patch("conix.resilience.supervisor.advance", side_effect=advanced):
            await asyncio.wait_for(follow("catalog", "dsn", stop=stop, poll_interval=30), 1)

        self.assertEqual(calls, [("catalog", "dsn")] * 3)

    async def test_idle_wait_releases_control_immediately_on_stop(self):
        stop = asyncio.Event()
        at_tip = asyncio.Event()
        calls = 0

        async def advanced(_catalog, _database_dsn):
            nonlocal calls
            calls += 1
            at_tip.set()
            return None

        with patch("conix.resilience.supervisor.advance", side_effect=advanced):
            task = asyncio.create_task(follow(None, None, stop=stop, poll_interval=30))
            try:
                await asyncio.wait_for(at_tip.wait(), 1)
                await asyncio.sleep(0)
                self.assertEqual(calls, 1)
            finally:
                stop.set()
                await asyncio.wait_for(task, 1)
        self.assertEqual(calls, 1)

    async def test_resumes_after_a_tip_poll(self):
        stop = asyncio.Event()
        at_tip = asyncio.Event()
        new_block = asyncio.Event()
        resumed = asyncio.Event()

        async def advanced(_catalog, _database_dsn):
            if not new_block.is_set():
                at_tip.set()
                return None
            resumed.set()
            stop.set()
            return object()

        with patch("conix.resilience.supervisor.advance", side_effect=advanced):
            task = asyncio.create_task(follow(None, None, stop=stop, poll_interval=0.02))
            try:
                await asyncio.wait_for(at_tip.wait(), 1)
                new_block.set()
                await asyncio.wait_for(resumed.wait(), 1)
            finally:
                stop.set()
                await asyncio.wait_for(task, 1)

    async def test_fails_without_retrying_or_masking_the_cause(self):
        stop = asyncio.Event()
        cause = RuntimeError("no automatic recovery yet")
        with patch("conix.resilience.supervisor.advance", side_effect=cause) as advanced:
            with self.assertRaises(RuntimeError) as caught:
                await follow(None, None, stop=stop, poll_interval=0.02)
        self.assertIs(caught.exception, cause)
        advanced.assert_awaited_once()

    async def test_cancellation_propagates_from_an_active_advance(self):
        entered = asyncio.Event()

        async def blocked(_catalog, _database_dsn):
            entered.set()
            await asyncio.Future()

        with patch("conix.resilience.supervisor.advance", side_effect=blocked) as advanced:
            task = asyncio.create_task(follow(None, None, stop=asyncio.Event()))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        advanced.assert_awaited_once()

    async def test_invalid_poll_interval_cannot_create_a_busy_loop(self):
        with patch("conix.resilience.supervisor.advance") as advanced:
            for interval in (0, -1):
                with self.subTest(interval=interval), self.assertRaises(ValueError):
                    await follow(None, None, stop=asyncio.Event(), poll_interval=interval)
            advanced.assert_not_called()


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL")
    and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires the owner-initialized disposable Anvil and PostgreSQL source",
)
class ResilienceAnvilContracts(unittest.IsolatedAsyncioTestCase):
    async def test_follows_real_events_and_empty_blocks_through_tier_zero(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        runtime_dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        stop = asyncio.Event()
        at_tip = asyncio.Event()
        resumed = asyncio.Event()
        second_height = None

        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def rpc(method, params):
                response = await http.post(url, json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
                })
                response.raise_for_status()
                envelope = response.json()
                self.assertNotIn("error", envelope)
                return envelope["result"]

            genesis = await rpc("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)

            # Anvil test-only bytecode emits the independent ERC-20 Transfer
            # vector. Neither the supervisor nor Tier 0 submits transactions.
            runtime = (
                b"\x60\x07\x60\x00\x52"
                + b"\x7f" + ERC20.topics[2]
                + b"\x7f" + ERC20.topics[1]
                + b"\x7f" + ERC20.topics[0]
                + b"\x60\x20\x60\x00\xa3\x00"
            )
            token = "0x" + catalog.contract("token").address.hex()
            await rpc("anvil_setCode", [token, "0x" + runtime.hex()])
            (sender, *_) = await rpc("eth_accounts", [])
            transaction = await rpc("eth_sendTransaction", [{
                "from": sender, "to": token, "gas": "0x100000",
            }])
            async with asyncio.timeout(5):
                while (receipt := await rpc("eth_getTransactionReceipt", [transaction])) is None:
                    await asyncio.sleep(0.02)
            self.assertEqual(receipt["status"], "0x1")
            self.assertEqual(len(receipt["logs"]), 1)
            first_height = int(receipt["blockNumber"], 16)

            async def observed_advance(source, database_dsn):
                progress = await advance(source, database_dsn)
                if progress is None:
                    at_tip.set()
                elif second_height is not None and progress.last_block.number == second_height:
                    resumed.set()
                return progress

            # The patched methods continue into the real installed core.
            with (
                patch("conix.resilience.supervisor.advance", side_effect=observed_advance),
                patch.object(Writer, "advance_one", autospec=True, side_effect=Writer.advance_one) as scanning,
                patch.object(Journal, "publish_block", autospec=True, side_effect=Journal.publish_block) as publishing,
            ):
                task = asyncio.create_task(follow(catalog, runtime_dsn, stop=stop, poll_interval=0.05))
                try:
                    await asyncio.wait_for(at_tip.wait(), 30)
                    await rpc("evm_mine", [])
                    second_height = first_height + 1
                    await asyncio.wait_for(resumed.wait(), 10)
                finally:
                    stop.set()
                    await asyncio.wait_for(task, 10)

            self.assertGreaterEqual(scanning.await_count, 3)
            self.assertGreaterEqual(publishing.await_count, 2)

            async with open_reader(
                catalog=catalog,
                database_dsn=os.environ["CONIX_TEST_ANVIL_READ_DSN"],
                rpc_url=url,
            ) as reader:
                checkpoint = await reader.checkpoint()
                self.assertEqual(checkpoint.block.number, second_height)
                page = await reader.events(
                    checkpoint=checkpoint, contract="token", event=TRANSFER_SIGNATURE,
                    where={"to": TO}, limit=catalog.limits.max_page_size,
                )
                self.assertIn(bytes.fromhex(transaction[2:]),
                              [event.transaction_hash for event in page.events])


if __name__ == "__main__":
    unittest.main()
