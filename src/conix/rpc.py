"""Bounded HTTP JSON-RPC for one explicitly selected EVM endpoint.

Only this transport handles HTTP, JSON-RPC request identity and response
limits. It neither interprets EVM events nor retries an uncertain operation.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from conix.abi import ResourceExceeded
from conix.catalog import Limits


class RpcUnavailable(RuntimeError):
    """The selected provider did not complete the requested observation."""


class RpcProtocolError(RuntimeError):
    """An RPC reply cannot be trusted as the result of its selected request."""


class RPC:
    """Send bounded requests through one connection with no ambient proxies."""

    def __init__(self, *, client: httpx.AsyncClient, url: str, limits: Limits):
        self.client = client
        self.url = url
        self.limits = limits
        self.next_id = 0

    async def call(self, method: str, params: list, *, deadline: float) -> object:
        """Return one correlated result, refusing partial or oversized replies."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RpcUnavailable("The selected indexing operation deadline expired.")

        self.next_id += 1
        request_id = self.next_id
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        timeout_seconds = min(self.limits.rpc_timeout_seconds, remaining)

        try:
            async with asyncio.timeout(timeout_seconds):
                async with self.client.stream(
                    "POST",
                    self.url,
                    json=request,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        raise RpcUnavailable(
                            f"RPC {method} returned HTTP {response.status_code}."
                        )

                    content = bytearray()
                    # Bound received bytes before any decompression. A
                    # provider ignoring identity encoding cannot turn a
                    # small compressed response into unbounded JSON memory.
                    async for chunk in response.aiter_raw():
                        content.extend(chunk)
                        if len(content) > self.limits.max_response_bytes:
                            raise ResourceExceeded("RPC response exceeds its configured byte budget.")
        except TimeoutError as error:
            raise RpcUnavailable(f"RPC {method} exceeded its request deadline.") from error
        except httpx.RequestError as error:
            raise RpcUnavailable(f"RPC {method} failed ({type(error).__name__}).") from error

        try:
            document = json.loads(content.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise RpcProtocolError(f"RPC {method} returned malformed JSON.") from error

        if (
            type(document) is not dict
            or document.get("jsonrpc") != "2.0"
            or type(document.get("id")) is not int
            or document["id"] != request_id
            or ("result" in document) == ("error" in document)
        ):
            raise RpcProtocolError(f"RPC {method} returned an uncorrelated envelope.")

        if "error" in document:
            # Provider error text may contain endpoint credentials or request
            # details; its semantics are an unavailable observation here.
            raise RpcUnavailable(f"RPC {method} was rejected by the selected provider.")

        return document["result"]
