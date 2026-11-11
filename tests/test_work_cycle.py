"""One session per finite cycle; every publication remains a Tier-0 advance."""

import asyncio
from contextlib import asynccontextmanager
import os
import unittest
from unittest.mock import Mock, patch

import httpx

from anvil_catalog import catalog_for_anvil
from conix.reader import open_reader
from conix.work_cycle import run_cycle
from conix.worker import advance


class CycleSchedulingContracts(unittest.IsolatedAsyncioTestCase):
    """Isolate scheduling policy while keeping the resource lifetime visible."""

    async def run_steps(self, steps, *, blocks=3, duration=60):
        writer = Mock()
        writer.advance_one = unittest.mock.AsyncMock(side_effect=steps)
        released = []

        @asynccontextmanager
        async def session(**_kwargs):
            try:
                yield writer
            finally:
                released.append(True)

        catalog = Mock()
        catalog.chain.rpc_url = "http://not-used"
        catalog.limits.operation_timeout_seconds = 3
        with patch("conix.work_cycle.open_writer", side_effect=session):
            outcome = await run_cycle(
                catalog, "dsn", max_blocks=blocks, max_duration=duration,
            )
        self.assertEqual(released, [True])
        return (outcome, writer)

    async def test_a_budget_finishes_without_extra_advances(self):
        """Reaching a block budget must retire the real writer session."""
        one, two = object(), object()
        (outcome, writer) = await self.run_steps([one, two], blocks=2)
        self.assertEqual(outcome.reason, "budget_exhausted")
        self.assertEqual(outcome.advanced_blocks, 2)
        self.assertIs(outcome.last_progress, two)
        self.assertEqual(writer.advance_one.await_count, 2)

    async def test_tip_ends_without_sleeping_or_fabricating_progress(self):
        """An observed tip, unlike exhausted budget, confirms no next block."""
        one = object()
        (outcome, writer) = await self.run_steps([one, None], blocks=4)
        self.assertEqual(outcome.reason, "at_tip")
        self.assertEqual(outcome.advanced_blocks, 1)
        self.assertIs(outcome.last_progress, one)
        self.assertEqual(writer.advance_one.await_count, 2)

    async def test_failure_does_not_turn_prior_publication_into_cycle_success(self):
        """A failed second advance aborts the cycle and closes its writer."""
        cause = RuntimeError("publication uncertain")
        writer = Mock()
        writer.advance_one = unittest.mock.AsyncMock(side_effect=[object(), cause])
        released = []

        @asynccontextmanager
        async def session(**_kwargs):
            try:
                yield writer
            finally:
                released.append(True)

        catalog = Mock()
        catalog.chain.rpc_url = "http://not-used"
        catalog.limits.operation_timeout_seconds = 3
        with patch("conix.work_cycle.open_writer", side_effect=session):
            with self.assertRaises(RuntimeError) as error:
                await run_cycle(catalog, "dsn", max_blocks=4, max_duration=30)
        self.assertIs(error.exception, cause)
        self.assertEqual(writer.advance_one.await_count, 2)
        self.assertEqual(released, [True])

    async def test_cancelled_advance_retires_the_session(self):
        """Cancellation propagates; it never produces a success receipt."""
        writer = Mock()
        writer.advance_one = unittest.mock.AsyncMock(side_effect=asyncio.CancelledError)
        released = []

        @asynccontextmanager
        async def session(**_kwargs):
            try:
                yield writer
            finally:
                released.append(True)

        catalog = Mock()
        catalog.chain.rpc_url = "http://not-used"
        catalog.limits.operation_timeout_seconds = 3
        with patch("conix.work_cycle.open_writer", side_effect=session):
            with self.assertRaises(asyncio.CancelledError):
                await run_cycle(catalog, "dsn", max_blocks=4, max_duration=30)
        self.assertEqual(released, [True])

    async def test_invalid_budgets_never_open_the_writer(self):
        """Reject nonfinite and nonpositive cycle budgets at admission."""
        catalog = Mock()
        for blocks, duration in ((0, 5), (True, 5), (1, 0), (1, float("inf")),
                                 (1, float("nan")), (1, True)):
            with self.subTest(blocks=blocks, duration=duration):
                with patch("conix.work_cycle.open_writer") as opened:
                    with self.assertRaises(ValueError):
                        await run_cycle(catalog, "dsn", max_blocks=blocks, max_duration=duration)
                    opened.assert_not_called()


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires owner-initialized disposable Anvil and PostgreSQL",
)
class CycleAnvilContracts(unittest.IsolatedAsyncioTestCase):
    """A real cycle delegates to the existing scanner and journal."""

    async def test_two_cycles_publish_real_empty_blocks_and_close_at_tip(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def rpc(method, params):
                response = await http.post(url, json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
                })
                response.raise_for_status()
                message = response.json()
                self.assertNotIn("error", message)
                return message["result"]

            genesis = await rpc("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)

            # Begin from durable progress even if earlier tests already used
            # this selected chain. No direct SQL initialization or mutations.
            async with asyncio.timeout(30):
                while await advance(catalog, dsn) is not None:
                    pass

            await rpc("evm_mine", [])
            await rpc("evm_mine", [])
            head = await rpc("eth_getBlockByNumber", ["latest", False])

            first = await run_cycle(catalog, dsn, max_blocks=1, max_duration=10)
            self.assertEqual(first.reason, "budget_exhausted")
            self.assertEqual(first.advanced_blocks, 1)

            second = await run_cycle(catalog, dsn, max_blocks=4, max_duration=10)
            self.assertEqual(second.reason, "at_tip")
            self.assertEqual(second.advanced_blocks, 1)
            self.assertEqual(second.last_progress.last_block.hash.hex(), head["hash"][2:].lower())

            async with open_reader(
                catalog=catalog, database_dsn=os.environ["CONIX_TEST_ANVIL_READ_DSN"],
                rpc_url=url,
            ) as reader:
                checkpoint = await reader.checkpoint()
                self.assertEqual(checkpoint.block.hash.hex(), head["hash"][2:].lower())


if __name__ == "__main__":
    unittest.main()
