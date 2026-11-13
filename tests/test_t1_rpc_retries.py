"""T1-A retries explicitly transient RPC reads without replacing Tier-0.

These tests exercise the real installed RPC decoder and Writer.advance_one,
plus the unchanged application's injected writer-session boundary. The
retry policy must never manufacture a successful evidence/publication result.
"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import httpx

from conix import app
from conix.catalog import prepare_catalog
from conix.core import Writer
from conix.rpc import RPC, RpcProtocolError, RpcUnavailable
from conix.resilience.rpc_retry import RetryingRPC, retrying_writer_session
from test_core_rpc import RecordingJournal, Server, json_response


FIXTURE = Path("/tests/fixtures/catalog.toml")


def response(request, *, result="0x7a69"):
    message = json.loads(request.content)
    return json_response({
        "jsonrpc": "2.0", "id": message["id"], "result": result,
    })


class RetryingReadContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = prepare_catalog(FIXTURE)

    async def client(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, timeout=None)
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_only_explicit_transport_failures_retry_the_same_read(self):
        for error_type in (httpx.ConnectError, httpx.ConnectTimeout,
                           httpx.ReadError, httpx.ReadTimeout):
            with self.subTest(error=error_type.__name__):
                requests = []

                def handler(request):
                    payload = json.loads(request.content)
                    requests.append((payload["method"], payload["params"], payload["id"]))
                    if len(requests) < 3:
                        raise error_type("temporary unavailable", request=request)
                    return response(request, result={"number": "0x1"})

                selected_url = "http://source.invalid/selected"
                base = RPC(
                    client=await self.client(handler), url=selected_url,
                    limits=self.catalog.limits,
                )
                retry = RetryingRPC(base)
                original = time.monotonic() + 2.0
                value = await retry.call(
                    "eth_getBlockByNumber", ["0x1", False], deadline=original,
                )
                self.assertEqual(value, {"number": "0x1"})
                self.assertEqual(len(requests), 3)
                self.assertEqual(
                    [(method, params) for method, params, _ in requests],
                    [("eth_getBlockByNumber", ["0x1", False])] * 3,
                )
                self.assertEqual(len({identifier for _, _, identifier in requests}), 3)
                self.assertEqual(base.url, selected_url)

    async def test_provider_json_rejection_and_http_status_are_not_retried(self):
        for mode in ("json_error", "http_429", "http_503", "bad_json", "wrong_id"):
            with self.subTest(mode=mode):
                attempts = []

                def handler(request):
                    attempts.append(request)
                    if mode == "http_429":
                        return httpx.Response(429, text="Retry later")
                    if mode == "http_503":
                        return httpx.Response(503, text="unavailable")
                    if mode == "bad_json":
                        return httpx.Response(
                            200, stream=httpx.ByteStream(b'{"jsonrpc":}'),
                        )
                    message = json.loads(request.content)
                    if mode == "wrong_id":
                        return json_response({
                            "jsonrpc": "2.0", "id": message["id"] + 1, "result": "0x1",
                        })
                    return json_response({
                        "jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32000, "message": "private provider secret"},
                    })

                base = RPC(client=await self.client(handler),
                           url="http://source.invalid/", limits=self.catalog.limits)
                with self.assertRaises((RpcUnavailable, RpcProtocolError)) as failure:
                    await RetryingRPC(base).call(
                        "eth_getLogs", [{"blockHash": "0x" + "a" * 64}], deadline=time.monotonic() + 2,
                    )
                self.assertEqual(len(attempts), 1)
                self.assertNotIn("private provider secret", str(failure.exception))

    async def test_expired_absolute_deadline_cannot_start_or_extend_a_read(self):
        requests = []
        def handler(request):
            requests.append(request)
            return response(request)
        base = RPC(client=await self.client(handler), url="http://source.invalid/",
                   limits=self.catalog.limits)
        original = time.monotonic() - 0.001
        with self.assertRaises(RpcUnavailable):
            await RetryingRPC(base).call("eth_getLogs", [], deadline=original)
        self.assertEqual(requests, [])

    async def test_fixed_original_deadline_and_bounded_attempts(self):
        calls = []
        cause = httpx.ReadError("read unavailable")

        class FailingRead:
            async def call(self, method, params, *, deadline):
                calls.append((method, params, deadline))
                raise RpcUnavailable("RPC eth_getLogs failed (ReadError).") from cause

        original = time.monotonic() + 0.30
        with self.assertRaises(RpcUnavailable) as failure:
            await RetryingRPC(FailingRead()).call(
                "eth_getLogs", [{"blockHash": "0x" + "1" * 64}], deadline=original,
            )
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(deadline == original for _, _, deadline in calls))
        self.assertIs(failure.exception.__cause__, cause)

    async def test_cancellation_during_retry_wait_stops_new_attempts(self):
        entered = asyncio.Event()
        attempts = []

        class FailingRead:
            async def call(self, method, params, *, deadline):
                attempts.append(method)
                entered.set()
                raise RpcUnavailable("RPC unavailable") from httpx.ConnectError("connection lost")

        task = asyncio.create_task(RetryingRPC(FailingRead()).call(
            "eth_getLogs", [], deadline=time.monotonic() + 2,
        ))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.12)
        self.assertEqual(attempts, ["eth_getLogs"])

    async def test_cancellation_of_inflight_read_stops_attempts(self):
        entered = asyncio.Event()
        attempts = []

        class SuspendedRead:
            async def call(self, method, params, *, deadline):
                attempts.append((method, deadline))
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(RetryingRPC(SuspendedRead()).call(
            "eth_getBlockByNumber", ["0x1", False], deadline=time.monotonic() + 2,
        ))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(attempts), 1)

    async def test_no_transient_retry_for_unselected_or_write_rpc_methods(self):
        attempts = []

        class Rejected:
            async def call(self, method, params, *, deadline):
                attempts.append(method)
                raise RpcUnavailable("unavailable") from httpx.ConnectError("temporary")

        for method in ("eth_sendRawTransaction", "eth_chainId", "eth_call"):
            with self.subTest(method=method), self.assertRaises(RpcUnavailable):
                await RetryingRPC(Rejected()).call(method, [], deadline=time.monotonic() + 2)
        self.assertEqual(attempts, ["eth_sendRawTransaction", "eth_chainId", "eth_call"])

    async def test_real_tier_zero_validator_and_publisher_still_execute(self):
        source = self.catalog
        oracle = Server()
        journal = RecordingJournal(source)
        requests = []

        def handler(request):
            message = json.loads(request.content)
            requests.append((message["method"], message["params"]))
            if message["method"] == "eth_getLogs" and sum(
                method == "eth_getLogs" for method, _ in requests
            ) == 1:
                raise httpx.ConnectError("temporary", request=request)
            return oracle(request)

        client = await self.client(handler)
        base = RPC(client=client, url="http://source.invalid/", limits=source.limits)
        writer = Writer(catalog=source, journal=journal, rpc=RetryingRPC(base))
        await writer.verify_source()
        progress = await writer.advance_one()

        self.assertEqual(progress.last_block.number, 1)
        self.assertEqual(len(journal.publications), 1)
        self.assertEqual(len(journal.publications[0][1]), 1)
        pinned_filters = [params for method, params in requests if method == "eth_getLogs"]
        self.assertGreaterEqual(len(pinned_filters), 2)
        self.assertEqual(pinned_filters[0], pinned_filters[1])
        self.assertEqual(
            pinned_filters[0][0]["blockHash"],
            "0x" + "11" * 32,
        )


class RetryingSessionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_session_wraps_the_actual_opened_writer_after_source_admission(self):
        opened = []
        closed = []

        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        selected = SimpleNamespace(catalog=object(), journal=object(), rpc=object())
        @asynccontextmanager
        async def actual_writer(**_kwargs):
            opened.append(selected)
            try:
                yield selected
            finally:
                closed.append(selected)

        with patch("conix.resilience.rpc_retry.open_writer", actual_writer):
            async with retrying_writer_session(
                catalog=selected.catalog, database_dsn="private",
                rpc_url="http://source.invalid",
            ) as writer:
                self.assertIsInstance(writer, Writer)
                self.assertIs(writer.catalog, selected.catalog)
                self.assertIs(writer.journal, selected.journal)
                self.assertIs(writer.rpc.base, selected.rpc)
                self.assertIsInstance(writer.rpc, RetryingRPC)
        self.assertEqual(closed, opened)
        self.assertIs(selected.rpc, opened[0].rpc)

    async def test_injected_writer_session_runs_the_original_app_loop(self):
        from contextlib import asynccontextmanager
        from io import StringIO
        from contextlib import redirect_stdout
        from types import SimpleNamespace

        stop = asyncio.Event()
        calls = []
        opened = []
        result = SimpleNamespace(
            source_id=b"x" * 32, revision=1,
            last_block=SimpleNamespace(number=1, hash=b"a" * 32),
        )

        @asynccontextmanager
        async def session(**_kwargs):
            opened.append(True)
            class Writer:
                async def advance_one(self):
                    calls.append(True)
                    stop.set()
                    return result
            yield Writer()

        source = SimpleNamespace(
            chain=SimpleNamespace(rpc_url="http://source.invalid"),
            limits=SimpleNamespace(operation_timeout_seconds=1),
        )
        with redirect_stdout(StringIO()) as output:
            await app.run(
                source, "runtime-dsn", stop, writer_session=session,
            )
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output.getvalue().count('"outcome": "advanced"'), 1)


if __name__ == "__main__":
    unittest.main()
