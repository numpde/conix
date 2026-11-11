"""Withhold one PostgreSQL COMMIT completion in the disposable SQL lane."""

import asyncio
from contextlib import asynccontextmanager, suppress

from psycopg.conninfo import make_conninfo


@asynccontextmanager
async def without_commit_reply(runtime_dsn):
    """Own a one-connection relay from loopback to the fixed Compose service."""
    accepted = asyncio.get_running_loop().create_future()

    def accept(reader, writer):
        if accepted.done():
            writer.close()
            return
        accepted.set_result((reader, writer))
        listener.close()

    listener = await asyncio.start_server(accept, "127.0.0.1", 0, start_serving=False)
    async with listener:
        try:
            async with asyncio.TaskGroup() as tasks:
                port = listener.sockets[0].getsockname()[1]
                # The disposable fixture uses plaintext message framing.
                # Neither connection info nor frames are logged.
                dsn = make_conninfo(runtime_dsn, host="127.0.0.1", hostaddr="127.0.0.1",
                                    port=port, sslmode="disable", gssencmode="disable")
                dropped = tasks.create_task(_relay(accepted))
                await listener.start_serving()
                yield (dsn, dropped)
        finally:
            # An accept can complete just before the relay task is cancelled.
            if accepted.done() and not accepted.cancelled():
                (_, writer) = accepted.result()
                if not writer.is_closing():
                    writer.close()
                    with suppress(ConnectionError):
                        await writer.wait_closed()


async def _relay(accepted):
    (client_reader, client_writer) = await accepted
    database_writer = None
    try:
        (database_reader, database_writer) = await asyncio.open_connection("postgres", 5432)
        async with asyncio.TaskGroup() as tasks:
            frontend = tasks.create_task(_forward_client(client_reader, database_writer))
            try:
                await _withhold_commit(database_reader, client_writer)
            finally:
                frontend.cancel()
    finally:
        client_writer.close()
        if database_writer is not None:
            database_writer.close()
        with suppress(ConnectionError):
            await client_writer.wait_closed()
        if database_writer is not None:
            with suppress(ConnectionError):
                await database_writer.wait_closed()


async def _forward_client(reader, writer):
    while chunk := await reader.read(16384):
        writer.write(chunk)
        await writer.drain()


async def _withhold_commit(reader, writer):
    """Forward complete backend frames until the server completes COMMIT."""
    try:
        while True:
            header = await reader.readexactly(5)
            length = int.from_bytes(header[1:], "big")
            if not 4 <= length <= 65536:
                raise AssertionError("Unexpected PostgreSQL fixture frame length.")
            payload = await reader.readexactly(length - 4)
            if header[:1] == b"C" and payload == b"COMMIT\0":
                return
            writer.write(header + payload)
            await writer.drain()
    except asyncio.IncompleteReadError:
        raise AssertionError("PostgreSQL disconnected before the COMMIT completion frame.") from None
