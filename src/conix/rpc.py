# Adapted from numpde/pay-to-play@2a1adb00b2eba9a18b49c92ca39a69ecf59bb110
# src/local_observer/rpc.py; shared protocol/ABI semantics.
"""Bounded asynchronous Ethereum JSON-RPC transport; no contract interpretation.

The caller owns the HTTPX client, including its pool, transport/authentication
configuration and shutdown. Requests never follow redirects. Local composition
disables ambient proxy configuration. Remote error prose is not included in our
exceptions.
"""

import asyncio
import json
import math

import httpx

from conix.models import Block


class RpcUnavailable(Exception):
    """A transport or upstream response prevented an RPC result from being obtained."""


class RpcProtocolError(Exception):
    """The source returned an invalid or unsuccessful JSON-RPC envelope."""


class RpcError(Exception):
    """An upstream JSON-RPC error, retaining its numeric code without remote prose."""

    def __init__(self, method: str, code: int):
        self.method = method
        self.code = code
        super().__init__(f"RPC {method} returned error code {code}.")


class RpcClient:
    """Issue bounded calls through one caller-owned client and explicit endpoint."""

    def __init__(self, client: httpx.AsyncClient, url: str, *, timeout: float, max_response_bytes: int):
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.userinfo or parsed.fragment:
            raise ValueError("RPC endpoint must be an HTTP(S) URL without credentials or a fragment.")

        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError("RPC timeout must be a positive number of seconds, no greater than 60.")

        if type(max_response_bytes) is not int or not 0 < max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("RPC response limit must be a positive byte count, no greater than 4 MiB.")
        self._client = client
        self._url = parsed
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    async def call(self, method: str, params: list) -> object:
        """Return one bounded, matching response; never retry an unknown RPC effect."""
        if type(method) is not str or not method:
            raise TypeError("RPC method must be a nonempty string.")

        if type(params) is not list:
            raise TypeError("RPC params must be a list.")

        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, allow_nan=False).encode("utf-8")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    "POST",
                    self._url,
                    content=raw,
                    headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        raise RpcUnavailable(f"RPC {method} returned HTTP status {response.status_code}.")

                    encoding = response.headers.get("content-encoding")
                    if encoding is not None and encoding != "identity":
                        raise RpcProtocolError(f"RPC {method} returned an unsupported compressed response.")

                    # Size checks precede buffering so an untrusted server
                    # cannot exhaust memory by sending an oversized response.
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > self._max_response_bytes:
                            raise RpcProtocolError(f"RPC {method} response exceeded its byte limit.")

                        body.extend(chunk)
        except (httpx.HTTPError, TimeoutError) as error:
            raise RpcUnavailable(f"RPC {method} could not complete within its transport limits.") from error

        try:
            envelope = json.loads(
                bytes(body).decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite,
                parse_float=_finite_float,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            raise RpcProtocolError(f"RPC {method} returned invalid JSON.") from error

        if (
            type(envelope) is not dict
            or envelope.get("jsonrpc") != "2.0"
            or type(envelope.get("id")) is not int
            or envelope["id"] != 1
            or ("result" in envelope) == ("error" in envelope)
        ):
            raise RpcProtocolError(f"RPC {method} returned an invalid response envelope.")

        if "error" in envelope:
            # The RPC error's prose is untrusted; retain only its numeric code.
            error = envelope["error"]
            if type(error) is not dict or type(error.get("code")) is not int or type(error.get("message")) is not str:
                raise RpcProtocolError(f"RPC {method} returned a malformed error envelope.")

            raise RpcError(method, error["code"])
        return envelope["result"]


def _unique_object(pairs):
    """Duplicate JSON names would make source interpretation ambiguous."""
    fields = {}
    for (name, value) in pairs:
        if name in fields:
            raise ValueError("RPC response contains duplicate JSON names.")

        fields[name] = value
    return fields


def _nonfinite(_token):
    raise ValueError("RPC response contains a nonfinite JSON number.")


def _finite_float(token):
    """Reject JSON values outside finite binary64 even when syntactically valid."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("RPC JSON number exceeds the finite binary64 range.")

    return value


def quantity(value: object, label: str) -> int:
    """Read the canonical Ethereum hexadecimal quantity at the RPC boundary."""
    if type(value) is not str or not value.startswith("0x") or not value[2:]:
        raise RpcProtocolError(f"RPC {label} must be an Ethereum hexadecimal quantity.")

    digits = value[2:]
    if len(digits) > 64 or (len(digits) > 1 and digits[0] == "0") or any(c not in "0123456789abcdef" for c in digits):
        raise RpcProtocolError(f"RPC {label} must be a canonical uint256 hexadecimal quantity.")

    return int(digits, 16)


def data(value: object, label: str, *, size: int | None) -> bytes:
    """Read exact Ethereum byte data without changing case or trimming padding."""
    if type(value) is not str or not value.startswith("0x"):
        raise RpcProtocolError(f"RPC {label} must be hexadecimal byte data.")

    digits = value[2:]
    if len(digits) % 2 or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise RpcProtocolError(f"RPC {label} contains malformed hexadecimal byte data.")

    result = bytes.fromhex(digits)
    if size is not None and len(result) != size:
        raise RpcProtocolError(f"RPC {label} must contain exactly {size} bytes.")

    return result


def read_block(value: object) -> Block:
    """Project one complete Ethereum block identity, independently of any contract ABI."""
    if type(value) is not dict:
        raise RpcProtocolError("RPC did not return an identified block.")
    try:
        return Block(
            quantity(value["number"], "block number"),
            data(value["hash"], "block hash", size=32),
            data(value["parentHash"], "parent hash", size=32),
            quantity(value["timestamp"], "block timestamp"),
        )
    except KeyError as error:
        raise RpcProtocolError("RPC block is missing a required identity field.") from error
