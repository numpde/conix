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
        rpc_endpoint = httpx.URL(url)

        if rpc_endpoint.scheme not in ("http", "https") or not rpc_endpoint.host or rpc_endpoint.userinfo or rpc_endpoint.fragment:
            raise ValueError("RPC endpoint must be an HTTP(S) URL without credentials or a fragment.")

        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError("RPC timeout must be a positive number of seconds, no greater than 60.")

        if type(max_response_bytes) is not int or not 0 < max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("RPC response limit must be a positive byte count, no greater than 4 MiB.")

        self._client = client
        self._url = rpc_endpoint
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    async def call(self, method: str, params: list) -> object:
        """Return one bounded, matching response; never retry an unknown RPC effect."""
        if type(method) is not str or not method:
            raise TypeError("RPC method must be a nonempty string.")

        if type(params) is not list:
            raise TypeError("RPC params must be a list.")

        request_bytes = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, allow_nan=False).encode("utf-8")

        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    method="POST",
                    url=self._url,
                    content=request_bytes,
                    headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        raise RpcUnavailable(f"RPC {method} returned HTTP status {response.status_code}.")

                    content_encoding = response.headers.get("content-encoding")

                    if content_encoding is not None and content_encoding != "identity":
                        raise RpcProtocolError(f"RPC {method} returned an unsupported compressed response.")

                    # Check size before buffering each chunk from the untrusted server.
                    response_bytes = bytearray()

                    async for response_chunk in response.aiter_raw():
                        if len(response_bytes) + len(response_chunk) > self._max_response_bytes:
                            raise RpcProtocolError(f"RPC {method} response exceeded its byte limit.")

                        response_bytes.extend(response_chunk)
        except (httpx.HTTPError, TimeoutError) as error:
            raise RpcUnavailable(f"RPC {method} could not complete within its transport limits.") from error

        try:
            rpc_response = json.loads(
                s=bytes(response_bytes).decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite,
                parse_float=_finite_float,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            raise RpcProtocolError(f"RPC {method} returned invalid JSON.") from error

        if (
            type(rpc_response) is not dict
            or rpc_response.get("jsonrpc") != "2.0"
            or type(rpc_response.get("id")) is not int
            or rpc_response["id"] != 1
            or ("result" in rpc_response) == ("error" in rpc_response)
        ):
            raise RpcProtocolError(f"RPC {method} returned an invalid response envelope.")

        if "error" in rpc_response:
            # An upstream error's prose may contain secrets. Retain only its code.
            rpc_error = rpc_response["error"]

            if type(rpc_error) is not dict or type(rpc_error.get("code")) is not int or type(rpc_error.get("message")) is not str:
                raise RpcProtocolError(f"RPC {method} returned a malformed error envelope.")

            raise RpcError(method, rpc_error["code"])

        return rpc_response["result"]


def _unique_object(pairs):
    """Reject ambiguous duplicate JSON object members."""
    json_object = {}

    for (name, value) in pairs:
        if name in json_object:
            raise ValueError("RPC response contains duplicate JSON names.")

        json_object[name] = value

    return json_object


def _nonfinite(_token):
    raise ValueError("RPC response contains a nonfinite JSON number.")


def _finite_float(token):
    """Reject syntactically valid JSON values outside finite binary64."""
    parsed_number = float(token)

    if not math.isfinite(parsed_number):
        raise ValueError("RPC JSON number exceeds the finite binary64 range.")

    return parsed_number


def quantity(value: object, label: str) -> int:
    """Read a canonical Ethereum hexadecimal uint256 quantity."""
    if type(value) is not str or not value.startswith("0x") or not value[2:]:
        raise RpcProtocolError(f"RPC {label} must be an Ethereum hexadecimal quantity.")

    hex_digits = value[2:]

    if len(hex_digits) > 64 or (len(hex_digits) > 1 and hex_digits[0] == "0") or any(
        digit not in "0123456789abcdef" for digit in hex_digits
    ):
        raise RpcProtocolError(f"RPC {label} must be a canonical uint256 hexadecimal quantity.")

    return int(hex_digits, 16)


def data(value: object, label: str, *, size: int | None) -> bytes:
    """Read exact Ethereum byte data without trimming or changing padding."""
    if type(value) is not str or not value.startswith("0x"):
        raise RpcProtocolError(f"RPC {label} must be hexadecimal byte data.")

    hex_digits = value[2:]

    if len(hex_digits) % 2 or any(digit not in "0123456789abcdefABCDEF" for digit in hex_digits):
        raise RpcProtocolError(f"RPC {label} contains malformed hexadecimal byte data.")

    decoded_bytes = bytes.fromhex(hex_digits)

    if size is not None and len(decoded_bytes) != size:
        raise RpcProtocolError(f"RPC {label} must contain exactly {size} bytes.")

    return decoded_bytes


def read_block(value: object) -> Block:
    """Read the complete block identity, independent of any contract ABI."""
    if type(value) is not dict:
        raise RpcProtocolError("RPC did not return an identified block.")

    try:
        return Block(
            number=quantity(value["number"], "block number"),
            hash=data(value["hash"], "block hash", size=32),
            parent_hash=data(value["parentHash"], "parent hash", size=32),
            timestamp=quantity(value["timestamp"], "block timestamp"),
        )
    except KeyError as error:
        raise RpcProtocolError("RPC block is missing a required identity field.") from error
