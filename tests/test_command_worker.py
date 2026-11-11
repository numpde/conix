"""Bounded command framing and concrete Tier-0 delegation in a persistent process."""

import asyncio
import io
import json
import os
import unittest
from unittest.mock import patch

import httpx

from anvil_catalog import catalog_for_anvil
from conix.command_worker import CommandProtocolError, _command, serve
from conix.journal import Block, CommitOutcomeUnknown, RetainedAncestry, Progress
from uuid import UUID
from conix.reader import open_reader
from conix.worker import progress_receipt


def frame(action):
    return (json.dumps({"version": 1, "action": action}) + "\n").encode("ascii")


class CommandWorkerContracts(unittest.IsolatedAsyncioTestCase):
    """Admission rejects malformed work before the qualified core runs."""

    def test_command_admission_requires_a_single_exact_frame(self):
        self.assertEqual(_command(frame("advance")), "advance")
        self.assertEqual(_command(frame("stop")), "stop")
        for invalid in (
            b"", b"{}", b"{}\n", b'{"version":1,"action":"reset"}\n',
            b'{"version":true,"action":"advance"}\n',
            b'{"version":1,"action":"advance","action":"stop"}\n',
            b'{"version":1,"action":"advance","unexpected":0}\n',
            frame("advance")[:-1], b"x" * 129,
        ):
            with self.subTest(frame=invalid), self.assertRaises(CommandProtocolError):
                _command(invalid)

    async def test_one_response_per_advance_and_explicit_stop(self):
        """Sequence a real caller interface; stop never invokes the worker."""
        catalog = unittest.mock.Mock()
        catalog.source_id = bytes.fromhex("a" * 64)
        first = unittest.mock.Mock()
        first.last_block.number = 4
        first.last_block.hash = bytes.fromhex("b" * 64)
        first.revision = 5

        output = io.BytesIO()
        commands = frame("advance") + frame("advance") + frame("stop") + frame("advance")
        with patch("conix.command_worker.advance", side_effect=(first, None)) as advanced:
            await serve(catalog, "dsn", incoming=io.BytesIO(commands), outgoing=output)

        self.assertEqual(advanced.await_count, 2)
        results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["outcome"] for row in results], ["advanced", "at_tip", "stopped"])
        self.assertEqual(results[0]["block_hash"], "0x" + "b" * 64)
        self.assertEqual(results[0]["revision"], 5)
        self.assertEqual(results[1]["source_id"], "a" * 64)

    async def test_inspect_reads_real_journal_facts_not_an_advance(self):
        """The command protocol cannot treat retained headers as canonical."""
        header = Block(12, bytes.fromhex("b" * 64), bytes.fromhex("c" * 64), 1234)
        progress = Progress(
            source_id=bytes.fromhex("a" * 64),
            generation=UUID("00000000-0000-0000-0000-000000000001"),
            history_start=1, revision=3, last_block=header,
            blocked_reason="repair required",
        )
        request = (json.dumps({
            "version": 1, "action": "inspect", "limit": 2, "through": 12,
        }) + "\n").encode("ascii")
        output = io.BytesIO()
        with (
            patch("conix.command_worker.inspect_retained_ancestry",
                  return_value=RetainedAncestry(progress, (header,))) as inspected,
            patch("conix.command_worker.advance") as advanced,
        ):
            await serve(None, "dsn",
                        incoming=io.BytesIO(request + frame("stop")), outgoing=output)
        inspected.assert_awaited_once_with(
            catalog=None, database_dsn="dsn", limit=2, through=12,
        )
        advanced.assert_not_awaited()
        (seen, stopped) = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(seen["outcome"], "inspected")
        self.assertTrue(seen["blocked"])
        self.assertEqual(seen["blocks"], [{"number": 12, "hash": "0x" + "b" * 64}])
        self.assertEqual(stopped, {"version": 1, "outcome": "stopped"})

    def test_inspect_cannot_widen_or_omit_its_bounded_scope(self):
        """Malformed scope is rejected before any journal inspection."""
        for request in (
            {"version": 1, "action": "inspect", "limit": 9, "through": None},
            {"version": 1, "action": "inspect", "limit": True, "through": None},
            {"version": 1, "action": "inspect", "limit": 1, "through": -1},
            {"version": 1, "action": "inspect", "limit": 1},
            {"version": 1, "action": "inspect", "limit": 1, "through": None, "extra": 0},
        ):
            with self.subTest(request=request), self.assertRaises(CommandProtocolError):
                _command((json.dumps(request) + "\n").encode("ascii"))

    async def test_unknown_commit_terminates_without_acknowledging_or_replaying(self):
        """Uncertain publication may not return success or read another request."""
        incoming = io.BytesIO(frame("advance") + frame("advance") + frame("stop"))
        outgoing = io.BytesIO()
        error = CommitOutcomeUnknown("unknown")
        with patch("conix.command_worker.advance", side_effect=error) as advanced:
            with self.assertRaises(CommitOutcomeUnknown) as caught:
                await serve(None, "dsn", incoming=incoming, outgoing=outgoing)
        self.assertIs(caught.exception, error)
        advanced.assert_awaited_once()
        self.assertEqual(outgoing.getvalue(), b"")

    async def test_invalid_frame_is_never_reinterpreted_as_a_later_command(self):
        """Malformed first input fails closed instead of scanning to a valid frame."""
        with patch("conix.command_worker.advance") as advanced:
            with self.assertRaises(CommandProtocolError):
                await serve(None, "dsn", incoming=io.BytesIO(b"garbage\n" + frame("advance")),
                            outgoing=io.BytesIO())
        advanced.assert_not_awaited()

    def test_progress_receipt_keeps_the_one_shot_schema(self):
        """Both Tier-0 entry points encode exactly the same outcome."""
        source = unittest.mock.Mock()
        source.source_id = bytes.fromhex("a" * 64)
        self.assertEqual(progress_receipt(source, None),
                         {"outcome": "at_tip", "source_id": "a" * 64})


@unittest.skipUnless(
    os.getenv("CONIX_TEST_ANVIL_URL") and os.getenv("CONIX_TEST_ANVIL_DSN")
    and os.getenv("CONIX_TEST_ANVIL_READ_DSN"),
    "Requires the owner-initialized disposable Anvil and PostgreSQL source",
)
class CommandWorkerAnvilContracts(unittest.IsolatedAsyncioTestCase):
    """The persistent protocol must publish through the installed scanner."""

    async def test_served_advance_reaches_real_anvil_and_persists_one_block(self):
        url = os.environ["CONIX_TEST_ANVIL_URL"]
        runtime_dsn = os.environ["CONIX_TEST_ANVIL_DSN"]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            async def rpc(method, params):
                response = await http.post(url, json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
                })
                response.raise_for_status()
                document = response.json()
                self.assertNotIn("error", document)
                return document["result"]

            genesis = await rpc("eth_getBlockByNumber", ["0x0", False])
            catalog = catalog_for_anvil(genesis_hash=genesis["hash"], rpc_url=url)

            # The shared initialized source may already contain earlier test
            # blocks. Catch up using ordinary core invocations, not SQL edits.
            from conix.worker import advance
            async with asyncio.timeout(30):
                while await advance(catalog, runtime_dsn) is not None:
                    pass

            before = await rpc("eth_getBlockByNumber", ["latest", False])
            await rpc("evm_mine", [])
            mined = await rpc("eth_getBlockByNumber", ["latest", False])
            self.assertEqual(int(mined["number"], 16), int(before["number"], 16) + 1)

            output = io.BytesIO()
            inspect = (json.dumps({
                "version": 1, "action": "inspect", "limit": 1, "through": None,
            }) + "\n").encode("ascii")
            await serve(catalog, runtime_dsn,
                        incoming=io.BytesIO(frame("advance") + inspect + frame("stop")),
                        outgoing=output)
            (advanced, ancestry, stopped) = [
                json.loads(line) for line in output.getvalue().splitlines()
            ]
            self.assertEqual(advanced["outcome"], "advanced")
            self.assertEqual(advanced["block_hash"], mined["hash"].lower())
            self.assertEqual(ancestry["outcome"], "inspected")
            self.assertEqual(ancestry["last_block_number"], int(mined["number"], 16))
            self.assertEqual(ancestry["blocks"], [{
                "number": int(mined["number"], 16),
                "hash": mined["hash"].lower(),
            }])
            self.assertEqual(stopped, {"version": 1, "outcome": "stopped"})

            async with open_reader(catalog=catalog,
                                   database_dsn=os.environ["CONIX_TEST_ANVIL_READ_DSN"],
                                   rpc_url=url) as reader:
                checkpoint = await reader.checkpoint()
                self.assertEqual(checkpoint.block.number, int(mined["number"], 16))
                self.assertEqual(checkpoint.block.hash.hex(), mined["hash"][2:].lower())


if __name__ == "__main__":
    unittest.main()
