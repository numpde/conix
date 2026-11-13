"""T1-B session supervisor: failure, retirement and cancellation contracts.

A session is the original app.run composition, NOT a second block scheduler.
The real PostgreSQL/Anvil witness separately proves concrete delegation.
"""

import asyncio
import unittest
from unittest.mock import patch

import httpx

from conix.journal import CommitOutcomeUnknown, HistoryConflict, StorageUnavailable
from conix.core import BranchConflict
from conix.rpc import RpcUnavailable, RpcProtocolError
from conix.resilience import supervisor


def unavailable_with(cause):
    error = RpcUnavailable("selected read failed")
    error.__cause__ = cause
    return error


class SessionSupervisorContracts(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_session_only_after_first_one_has_fully_retired(self):
        source = object()
        database_dsn = object()
        stop = asyncio.Event()
        running = 0
        retired = []
        received = []

        async def actual_session(catalog, dsn, signal):
            nonlocal running
            self.assertIs(catalog, source)
            self.assertIs(dsn, database_dsn)
            self.assertIs(signal, stop)
            self.assertEqual(running, 0)
            self.assertEqual(len(received), len(retired))
            running += 1
            received.append((catalog, dsn, signal))
            try:
                if len(received) == 1:
                    raise unavailable_with(httpx.ConnectError("offline"))
                stop.set()
            finally:
                running -= 1
                retired.append(len(received))

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            await supervisor.supervise(source, database_dsn, stop)

        self.assertEqual(len(received), 2)
        self.assertEqual(retired, [1, 2])
        self.assertEqual(running, 0)

    async def test_retry_budget_exhaustion_preserves_the_last_original_error(self):
        calls = []
        failures = [unavailable_with(httpx.ReadError("read lost")) for _ in range(3)]

        async def actual_session(*_args):
            calls.append(len(calls))
            raise failures[len(calls) - 1]

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            with self.assertRaises(RpcUnavailable) as caught:
                await supervisor.supervise(object(), object(), asyncio.Event())

        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(calls, [0, 1, 2])

    async def test_permanent_failures_do_not_restart(self):
        exceptions = [
            RpcUnavailable("selected provider refused request"),
            RpcUnavailable("selected indexing operation deadline expired"),
            RpcProtocolError("malformed JSON-RPC"),
            BranchConflict("wrong chain or canonical history"),
            HistoryConflict("blocked stored history"),
            StorageUnavailable("database unavailable or role rejected"),
            CommitOutcomeUnknown("lost SQL COMMIT confirmation"),
            TimeoutError("operation budget elapsed"),
            ValueError("invalid selected source"),
        ]

        for error in exceptions:
            with self.subTest(cause=type(error).__name__, detail=str(error)):
                attempts = []

                async def actual_session(*_args):
                    attempts.append(True)
                    raise error

                with patch.object(supervisor, "run_with_read_retries", actual_session):
                    with self.assertRaises(type(error)) as caught:
                        await supervisor.supervise(object(), object(), asyncio.Event())
                self.assertIs(caught.exception, error)
                self.assertEqual(attempts, [True])

    async def test_only_selected_transport_causes_allow_session_restart(self):
        eligible = (
            httpx.ConnectError("connect lost"),
            httpx.ConnectTimeout("connect timed out"),
            httpx.ReadError("read lost"),
            httpx.ReadTimeout("read timed out"),
        )

        for cause in eligible:
            with self.subTest(cause=type(cause).__name__):
                stop = asyncio.Event()
                attempts = []

                async def actual_session(*_args):
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise unavailable_with(cause)
                    stop.set()

                with patch.object(supervisor, "run_with_read_retries", actual_session):
                    await supervisor.supervise(object(), object(), stop)
                self.assertEqual(len(attempts), 2)

    async def test_request_timeout_is_not_a_session_restart_authority(self):
        # T1-A may retry a single bounded read after TimeoutError with
        # remaining deadline; at the session boundary that deadline is gone.
        error = unavailable_with(TimeoutError("request expired"))
        attempts = []

        async def actual_session(*_args):
            attempts.append(1)
            raise error

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            with self.assertRaises(RpcUnavailable) as caught:
                await supervisor.supervise(object(), object(), asyncio.Event())
        self.assertIs(caught.exception, error)
        self.assertEqual(attempts, [1])

    async def test_provider_http_rejection_with_no_transport_cause_is_permanent(self):
        error = unavailable_with(httpx.HTTPStatusError(
            "HTTP 503", request=httpx.Request("POST", "http://selected.invalid"),
            response=httpx.Response(503),
        ))
        calls = []

        async def actual_session(*_args):
            calls.append(True)
            raise error

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            with self.assertRaises(RpcUnavailable):
                await supervisor.supervise(object(), object(), asyncio.Event())
        self.assertEqual(calls, [True])

    async def test_stop_during_restart_delay_cannot_grant_a_new_session(self):
        stop = asyncio.Event()
        entered = asyncio.Event()
        attempts = []
        original = unavailable_with(httpx.ConnectError("unavailable"))

        async def actual_session(*_args):
            attempts.append(1)
            entered.set()
            raise original

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            task = asyncio.create_task(supervisor.supervise(object(), object(), stop))
            await asyncio.wait_for(entered.wait(), 1)
            stop.set()
            with self.assertRaises(RpcUnavailable) as caught:
                await asyncio.wait_for(task, 2)
        self.assertIs(caught.exception, original)
        self.assertEqual(attempts, [1])

    async def test_task_cancel_during_restart_delay_cannot_start_more_sessions(self):
        entered = asyncio.Event()
        calls = []

        async def actual_session(*_args):
            calls.append(True)
            entered.set()
            raise unavailable_with(httpx.ReadError("read failed"))

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            task = asyncio.create_task(supervisor.supervise(object(), object(), asyncio.Event()))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0.12)
        self.assertEqual(calls, [True])

    async def test_cancel_inflight_session_propagates_and_does_not_restart(self):
        entered = asyncio.Event()
        retired = asyncio.Event()
        calls = []

        async def actual_session(*_args):
            calls.append(True)
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                retired.set()

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            task = asyncio.create_task(supervisor.supervise(object(), object(), asyncio.Event()))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(calls, [True])
        self.assertTrue(retired.is_set())

    async def test_cleanup_failure_blocks_restart_even_after_transient_failure(self):
        calls = []
        uncertain_retirement = OSError("writer retirement not confirmed")

        async def actual_session(*_args):
            calls.append(True)
            try:
                raise unavailable_with(httpx.ConnectError("transient"))
            finally:
                raise uncertain_retirement

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            with self.assertRaises(OSError) as caught:
                await supervisor.supervise(object(), object(), asyncio.Event())
        self.assertIs(caught.exception, uncertain_retirement)
        self.assertEqual(calls, [True])

    async def test_clean_session_exit_is_not_restarted(self):
        calls = []

        async def actual_session(*_args):
            calls.append(True)

        with patch.object(supervisor, "run_with_read_retries", actual_session):
            await supervisor.supervise(object(), object(), asyncio.Event())
        self.assertEqual(calls, [True])

    async def test_original_app_loop_is_invoked_on_every_supervised_session(self):
        # Verify concrete application delegation through the retained T1-A
        # entrypoint; the next Anvil/PG suite establishes real publication.
        from conix.resilience import rpc_retry
        original = unavailable_with(httpx.ConnectError("transient"))
        stop = asyncio.Event()
        calls = []
        source = object()
        dsn = object()

        async def application(catalog, runtime, signal, *, poll_interval, writer_session):
            calls.append((catalog, runtime, signal, writer_session))
            self.assertIs(catalog, source)
            self.assertIs(runtime, dsn)
            self.assertIs(signal, stop)
            self.assertIs(writer_session, rpc_retry.retrying_writer_session)
            if len(calls) == 1:
                raise original
            stop.set()

        with patch.object(rpc_retry.app, "run", application):
            await supervisor.supervise(source, dsn, stop)

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
