"""Compare autonomous Tier-0 runner behavior without substituting core SQL tests.

These tests prove scheduling/resource lifetime; the installed Anvil suite
separately proves that the same writer performs real publication.
"""

import asyncio
from contextlib import asynccontextmanager, redirect_stdout
import io
import inspect
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from conix import app
from conix.rpc import RpcUnavailable


def progress(number):
    return SimpleNamespace(
        source_id=b"x" * 32,
        last_block=SimpleNamespace(number=number, hash=bytes([number]) * 32),
        revision=number,
    )


class StandaloneLoopContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = SimpleNamespace(
            source_id=b"x" * 32,
            chain=SimpleNamespace(rpc_url="http://test-source.invalid"),
            limits=SimpleNamespace(operation_timeout_seconds=0.05),
        )

    async def exercise(self, sequence, *, failure=None):
        stop = asyncio.Event()
        opened, closed, seen = [], [], []
        active = 0

        @asynccontextmanager
        async def session(**_kwargs):
            nonlocal active
            self.assertEqual(active, 0, "A second writer was opened before closing the first.")
            active += 1
            opened.append(len(opened) + 1)

            class Writer:
                async def advance_one(self_inner):
                    if failure is not None:
                        raise failure
                    result = sequence.pop(0)
                    seen.append(result)
                    if not sequence:
                        stop.set()
                    return result

            try:
                yield Writer()
            finally:
                active -= 1
                closed.append(len(closed) + 1)

        with tempfile.TemporaryDirectory() as directory:
            kwargs = {"poll_interval": 0.005}
            socket = Path(directory) / "control.sock"
            if "socket_path" in inspect.signature(app.run).parameters:
                kwargs["socket_path"] = socket

            with patch.object(app, "open_writer", session), redirect_stdout(io.StringIO()) as output:
                if failure is None:
                    await asyncio.wait_for(
                        app.run(self.catalog, "private-test-dsn", stop, **kwargs),
                        timeout=5,
                    )
                else:
                    with self.assertRaises(RpcUnavailable):
                        await asyncio.wait_for(
                            app.run(self.catalog, "private-test-dsn", stop, **kwargs),
                            timeout=5,
                        )

            self.assertFalse(socket.exists(), "A control socket was retained after stop/failure.")
        self.assertEqual(active, 0)
        self.assertEqual(len(opened), len(closed))
        return (opened, seen, output.getvalue())

    async def test_catchup_tip_later_block_and_clean_stop(self):
        opened, seen, output = await self.exercise(
            [progress(1), progress(2), None, progress(3)],
        )
        self.assertEqual(
            [x.last_block.number if x else None for x in seen],
            [1, 2, None, 3],
        )
        self.assertEqual(output.count('"outcome": "advanced"'), 3)
        # A opens four sessions, B/C two sessions: the distinction is real.
        if "Fresh core writer" in (app.run.__doc__ or ""):
            self.assertEqual(len(opened), 4)
        else:
            self.assertEqual(len(opened), 2)

    async def test_source_failure_exits_without_retry_or_false_receipt(self):
        opened, seen, output = await self.exercise(
            [], failure=RpcUnavailable("provider unavailable"),
        )
        self.assertEqual(len(opened), 1)
        self.assertFalse(seen)
        self.assertNotIn('"outcome": "advanced"', output)

    async def test_operation_deadline_releases_writer_without_success(self):
        stopped = asyncio.Event()
        released = asyncio.Event()

        @asynccontextmanager
        async def opened(**_kwargs):
            class Hung:
                async def advance_one(self):
                    await asyncio.Event().wait()
            try:
                yield Hung()
            finally:
                released.set()

        with tempfile.TemporaryDirectory() as directory:
            kwargs = {"poll_interval": 0.005}
            if "socket_path" in inspect.signature(app.run).parameters:
                kwargs["socket_path"] = Path(directory) / "control.sock"

            with patch.object(app, "open_writer", opened), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(
                        app.run(self.catalog, "secret-dsn", stopped, **kwargs),
                        timeout=1.0,
                    )
            self.assertTrue(released.is_set())
            self.assertNotIn('"outcome": "advanced"', output.getvalue())


class WriterCompositionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_session_uses_original_loop_and_retires(self):
        """A composed session supplies the writer while the core owns scheduling."""
        stop = asyncio.Event()
        retired = []

        @asynccontextmanager
        async def session(**_kwargs):
            class Writer:
                async def advance_one(self):
                    stop.set()
                    return None
            try:
                yield Writer()
            finally:
                retired.append(True)

        catalog = SimpleNamespace(
            chain=SimpleNamespace(rpc_url="http://test.invalid"),
            limits=SimpleNamespace(operation_timeout_seconds=1),
        )
        await app.run(catalog, "private-test-dsn", stop, writer_session=session)
        self.assertEqual(retired, [True])
