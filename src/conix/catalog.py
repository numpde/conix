"""Compile one immutable, offline EVM event catalog from TOML and local ABIs.

A catalog fixes the history source, selected ABI layouts and reader aliases.
Neither this module nor its constructor contacts RPC or PostgreSQL. Later
changes to local files cannot mutate the descriptors held by a live session.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import tomllib
from urllib.parse import urlsplit

from eth_utils import is_checksum_address

from conix.abi import ABIValueType, AbiLimits, ContractDescriptor, DEFAULT_LIMITS, compile_contract


MAX_CONFIG_BYTES = 64 * 1024
MAX_TOTAL_ABI_BYTES = 8 * 1024 * 1024
ALIAS = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
GENESIS = re.compile(r"0x[0-9a-f]{64}\Z")

LIMIT_CEILINGS = {
    "rpc_timeout_seconds": 60,
    "operation_timeout_seconds": 300,
    "max_response_bytes": 4194304,
    "max_connections": 32,
    "max_page_size": 500,
    "max_evidence_blocks": 64,
    "max_logs_per_block": 10000,
    "max_abi_bytes": DEFAULT_LIMITS.max_bytes,
    "max_abi_nesting": DEFAULT_LIMITS.max_nesting,
    "max_abi_elements": DEFAULT_LIMITS.max_elements,
}
TIMEOUTS = frozenset({"rpc_timeout_seconds", "operation_timeout_seconds"})


class ConfigurationError(ValueError):
    """A selected local catalog cannot represent the declared event history."""


@dataclass(frozen=True, slots=True)
class Chain:
    chain_id: int
    genesis_hash: bytes
    start_block: int
    rpc_url: str


@dataclass(frozen=True, slots=True)
class Database:
    dsn_file: str
    runtime_role: str


@dataclass(frozen=True, slots=True)
class Limits:
    rpc_timeout_seconds: float
    operation_timeout_seconds: float
    max_response_bytes: int
    max_connections: int
    max_page_size: int
    max_evidence_blocks: int
    max_logs_per_block: int
    max_abi_bytes: int
    max_abi_nesting: int
    max_abi_elements: int

    @property
    def abi(self) -> AbiLimits:
        return AbiLimits(
            max_bytes=self.max_abi_bytes,
            max_nesting=self.max_abi_nesting,
            max_elements=self.max_abi_elements,
        )


@dataclass(frozen=True, slots=True)
class Contract:
    alias: str
    address: bytes
    descriptor: ContractDescriptor


@dataclass(frozen=True, slots=True)
class Catalog:
    source_id: bytes
    chain: Chain
    database: Database
    limits: Limits
    contracts: tuple[Contract, ...]

    def contract(self, alias: str) -> Contract:
        for selected in self.contracts:
            if selected.alias == alias:
                return selected
        raise KeyError(f"Unknown contract alias {alias!r}.")


def _keys(document: object, expected: set[str], label: str) -> dict:
    if type(document) is not dict or set(document) != expected:
        raise ConfigurationError(f"{label} requires exactly {sorted(expected)!r}.")
    return document


def _quantity(value: object, label: str, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ConfigurationError(f"{label} must be an integer in [0, {maximum}].")
    return value


def _address(value: object) -> bytes:
    if type(value) is not str or ADDRESS.fullmatch(value) is None:
        raise ConfigurationError("Contract address must contain exactly 20 hexadecimal bytes.")

    digits = value[2:]
    if any(digit.isupper() for digit in digits) and not is_checksum_address(value):
        raise ConfigurationError("Mixed-case addresses require a valid EIP-55 checksum.")
    return bytes.fromhex(digits)


def _rpc_url(value: object) -> str:
    if type(value) is not str:
        raise ConfigurationError("RPC URL must be an absolute HTTP(S) address.")
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port is None or parsed.port > 0
    except ValueError:
        raise ConfigurationError("RPC URL contains an invalid network authority.") from None

    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(ord(character) < 32 for character in value)
    ):
        raise ConfigurationError("RPC URL requires HTTP(S), a host and no credentials or fragment.")
    return value


def _read(path: Path, limit: int) -> bytes:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ConfigurationError("Catalog input path must not contain symbolic links.")

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ConfigurationError("Catalog input must be a regular local file.")
            data = stream.read(limit + 1)
    except OSError as error:
        raise ConfigurationError(f"Catalog input could not be read: {type(error).__name__}.") from error

    if not 0 < len(data) <= limit:
        raise ConfigurationError("Catalog input is empty or exceeds its byte budget.")
    return data


def _json_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for (name, value) in pairs:
        if name in result:
            raise ConfigurationError("ABI JSON contains a repeated member.")
        result[name] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ConfigurationError("ABI JSON contains a nonfinite numeric constant.")


def _abi(path: Path, max_bytes: int) -> tuple[list, int]:
    source = _read(path, max_bytes)
    try:
        payload = json.loads(
            source.decode("utf-8"),
            object_pairs_hook=_json_pairs,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ConfigurationError("ABI JSON is not a supported UTF-8 document.") from error

    if type(payload) is not list:
        raise ConfigurationError("Contract ABI must be a JSON array.")
    return payload, len(source)


def _abi_path(configuration: Path, declared: object) -> Path:
    if type(declared) is not str or not declared or "\x00" in declared:
        raise ConfigurationError("ABI file must name one explicit local path.")

    path = Path(declared)
    if ".." in path.parts:
        raise ConfigurationError("ABI file may not traverse ancestor directories.")
    if not path.is_absolute():
        path = configuration.parent / path
    return path


def _limits(document: object) -> Limits:
    selected = _keys(document, set(LIMIT_CEILINGS), "limits")
    values = {}

    for (name, ceiling) in LIMIT_CEILINGS.items():
        value = selected[name]
        if name in TIMEOUTS:
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= ceiling:
                raise ConfigurationError(f"{name} must be a finite positive timeout no greater than {ceiling}.")
        elif type(value) is not int or not 1 <= value <= ceiling:
            raise ConfigurationError(f"{name} must be an integer in [1, {ceiling}].")
        values[name] = value

    return Limits(**values)


def _components(spec: ABIValueType) -> list[dict]:
    while spec.kind == "array":
        spec = spec.item
    if spec.kind != "tuple":
        return []

    return [
        {
            "name": argument.name,
            "type": argument.type.canonical,
            "components": _components(argument.type),
        }
        for argument in spec.components
    ]


def _source_id(chain: Chain, contracts: tuple[Contract, ...]) -> bytes:
    events = []
    for contract in contracts:
        for event in contract.descriptor.events:
            events.append({
                "address": "0x" + contract.address.hex(),
                "signature": event.signature,
                "anonymous": False,
                "inputs": [
                    {
                        "name": argument.name,
                        "type": argument.type.canonical,
                        "indexed": argument.indexed,
                        "components": _components(argument.type),
                    }
                    for argument in event.arguments
                ],
            })

    document = {
        "schema": "conix.source.v1",
        "chain_id": str(chain.chain_id),
        "genesis_hash": "0x" + chain.genesis_hash.hex(),
        "start_block": str(chain.start_block),
        "events": sorted(events, key=lambda event: (event["address"], event["signature"])),
    }
    canonical_bytes = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256(canonical_bytes).digest()


def prepare_catalog(config_path: str | Path) -> Catalog:
    """Snapshot one explicit source selection, without network or database access."""
    configuration = Path(config_path)
    try:
        document = tomllib.loads(_read(configuration, MAX_CONFIG_BYTES).decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError("Configuration is not valid UTF-8 TOML.") from error

    root = _keys(document, {"version", "chain", "contracts", "database", "limits"}, "configuration")
    if type(root["version"]) is not int or root["version"] != 1:
        raise ConfigurationError("Configuration requires version = 1.")

    chain_document = _keys(root["chain"], {"chain_id", "genesis_hash", "start_block", "rpc_url"}, "chain")
    genesis = chain_document["genesis_hash"]
    if type(genesis) is not str or GENESIS.fullmatch(genesis) is None:
        raise ConfigurationError("Genesis hash must be 32 lowercase hexadecimal bytes.")
    chain = Chain(
        chain_id=_quantity(chain_document["chain_id"], "chain_id"),
        genesis_hash=bytes.fromhex(genesis[2:]),
        start_block=_quantity(chain_document["start_block"], "start_block"),
        rpc_url=_rpc_url(chain_document["rpc_url"]),
    )

    database_document = _keys(root["database"], {"dsn_file", "runtime_role"}, "database")
    if database_document["dsn_file"] != "/run/conix/connection.dsn":
        raise ConfigurationError("Database DSN must be supplied through the selected private input volume.")
    if database_document["runtime_role"] != "conix_runtime":
        raise ConfigurationError("Runtime role must be conix_runtime.")
    database = Database(**database_document)
    limits = _limits(root["limits"])

    contract_documents = root["contracts"]
    if type(contract_documents) is not dict or not 1 <= len(contract_documents) <= 32:
        raise ConfigurationError("Select between one and 32 contracts.")

    contracts = []
    seen_addresses = set()
    total_events = 0
    total_abi_bytes = 0

    for (alias, raw_contract) in contract_documents.items():
        if ALIAS.fullmatch(alias) is None:
            raise ConfigurationError("Contract aliases must use lowercase identifiers.")

        selected = _keys(raw_contract, {"address", "abi_file", "events"}, f"contract {alias}")
        address = _address(selected["address"])
        if address in seen_addresses:
            raise ConfigurationError("The same deployed address may be selected only once.")
        seen_addresses.add(address)

        signatures = selected["events"]
        if type(signatures) is not list or not signatures or any(type(item) is not str for item in signatures):
            raise ConfigurationError("Selected event signatures must be a nonempty list of strings.")

        abi_path = _abi_path(configuration, selected["abi_file"])
        declarations, byte_count = _abi(abi_path, limits.max_abi_bytes)
        total_abi_bytes += byte_count
        if total_abi_bytes > MAX_TOTAL_ABI_BYTES:
            raise ConfigurationError("Aggregate catalog ABIs exceed 8 MiB.")

        compiled = compile_contract(declarations, tuple(signatures), limits=limits.abi)
        total_events += len(compiled.events)
        if total_events > 256:
            raise ConfigurationError("Select no more than 256 events across the catalog.")
        contracts.append(Contract(alias=alias, address=address, descriptor=compiled))

    selected_contracts = tuple(contracts)
    return Catalog(
        source_id=_source_id(chain, selected_contracts),
        chain=chain,
        database=database,
        limits=limits,
        contracts=selected_contracts,
    )
