"""C's private control socket changes scheduling, never chain authority."""

from contextlib import suppress
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from conix.control import Controls, request, serve


class LocalControlContracts(unittest.IsolatedAsyncioTestCase):
    async def test_private_socket_pause_single_step_resume_and_close(self):
        state = Controls()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            with patch("conix.control.SOCKET", path):
                async with serve(state, path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    current = await request("status")
                    self.assertEqual(current["state"], "running")
                    with self.assertRaises(ValueError):
                        await request("step")
                    paused = await request("pause")
                    self.assertEqual(paused["state"], "paused")
                    requested = await request("step")
                    self.assertTrue(requested["step_pending"])
                    self.assertTrue(state.step_requested)
                    # The executor clears its one-step permit after advancing.
                    state.step_requested = False
                    resumed = await request("resume")
                    self.assertEqual(resumed["state"], "running")
            self.assertFalse(path.exists())

    async def test_invalid_and_oversized_commands_do_not_change_authority(self):
        state = Controls()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            async with serve(state, path):
                import asyncio
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(b"x" * 50 + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
                self.assertEqual(response["outcome"], "invalid_command")
                self.assertFalse(state.paused)
                self.assertFalse(state.step_requested)
                writer.close()
                await writer.wait_closed()
