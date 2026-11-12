"""Optional local control plane for variant C.

An autonomous indexer remains the default. Commands can pause, resume or
request one block while paused; they never supply events, repair history,
change the provider, or bypass the installed core. The socket is private
to the container's runtime UID and is not exposed as a network service.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path


SOCKET = Path("/tmp/conix-control.sock")
COMMANDS = frozenset({"status", "pause", "resume", "step"})


class Controls:
    def __init__(self):
        self.paused = False
        self.step_requested = False
        self.last_committed = None
        self.changed = asyncio.Event()

    async def handle(self, reader, writer):
        try:
            try:
                request = await asyncio.wait_for(reader.readline(), timeout=2.0)
                command = request.decode("ascii").strip() if len(request) <= 32 else ""
            except (TimeoutError, UnicodeError, ValueError, asyncio.LimitOverrunError):
                command = ""

            if command == "pause":
                self.paused = True
                self.changed.set()
            elif command == "resume":
                self.paused = False
                self.step_requested = False
                self.changed.set()
            elif command == "step" and self.paused:
                self.step_requested = True
                self.changed.set()

            response = {
                "outcome": "ok" if command in COMMANDS and (
                    command != "step" or self.paused
                ) else "invalid_command",
                "state": "paused" if self.paused else "running",
                "step_pending": self.step_requested,
                "last_committed": self.last_committed,
            }
            writer.write(json.dumps(response).encode("ascii") + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def serve(controls, path=SOCKET):
    """Create and retire only this process's private Unix-domain socket."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError("Control socket already exists; refusing to replace it.")
    server = await asyncio.start_unix_server(controls.handle, path=str(path), limit=128)
    try:
        os.chmod(path, 0o600)
        yield
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


async def request(command):
    """Used by 'python -I -m conix control ...' INSIDE the worker container."""
    if command not in COMMANDS:
        raise ValueError("Unknown local indexer control command.")
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(SOCKET)), timeout=3.0,
    )
    try:
        writer.write((command + "\n").encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=3.0)
        line = await asyncio.wait_for(reader.readline(), timeout=3.0)
        if len(line) > 512 or not line.endswith(b"\n"):
            raise OSError("Local control service returned an invalid bounded reply.")
        reply = json.loads(line)
        if type(reply) is not dict or reply.get("outcome") != "ok":
            raise ValueError("Local control command was refused.")
        return reply
    finally:
        writer.close()
        await writer.wait_closed()
