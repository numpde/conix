"""Strict operator configuration; all work limits and identities are explicit."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import os
import re
import stat
import tomllib

import httpx
from eth_utils import is_checksum_address

from conix.models import Deployment
from conix.rpc import RpcClient
from conix.pay_to_play import PayToPlay


@dataclass(frozen=True, slots=True)
class Settings:
    deployment: Deployment
    start_block: int
    rpc_url: str
    database_dsn_file: Path
    runtime_role: str
    poll_seconds: float
    rpc_timeout_seconds: float
    max_response_bytes: int
    max_connections: int
    max_page_size: int
    max_evidence_blocks: int
    operation_timeout_seconds: float

    def __post_init__(self):
        if type(self.deployment.chain_id) is not int or not 0 <= self.deployment.chain_id < 2**63:
            raise ValueError("Source chain_id must be a nonnegative signed 64-bit integer.")
        if not is_checksum_address(self.deployment.address):
            raise ValueError("Source contract address must be EIP-55 checksummed.")
        if type(self.start_block) is not int or not 0 <= self.start_block < 2**63:
            raise ValueError("Source start_block must be a nonnegative signed 64-bit integer.")
        for name, maximum in (
            ("poll_seconds", 3600), ("rpc_timeout_seconds", 60),
            ("operation_timeout_seconds", 300),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= maximum:
                raise ValueError(f"Limits {name} must be positive and at most {maximum}.")
        for name, maximum in (
            ("max_response_bytes", 4 * 1024 * 1024),
            ("max_connections", 32),
            ("max_page_size", 10000),
            ("max_evidence_blocks", 50000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Limits {name} must be a positive integer no greater than {maximum}.")
        if not isinstance(self.database_dsn_file, Path) or not self.database_dsn_file.is_absolute():
            raise ValueError("Database dsn_file must be an absolute path.")
        if type(self.runtime_role) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.runtime_role) is None:
            raise ValueError("Database runtime_role must explicitly name a simple PostgreSQL login role.")
        url = httpx.URL(self.rpc_url)
        if url.scheme not in ("http", "https") or not url.host or url.userinfo or url.fragment:
            raise ValueError("RPC URL must be HTTP(S), with no embedded credentials or fragment.")


def load_settings(path: Path) -> Settings:
    """Refuse absent, unknown and incorrectly typed fields; never infer endpoints."""
    content = path.read_bytes()
    if len(content) > 64 * 1024:
        raise ValueError("Configuration exceeds 64 KiB.")
    fields = tomllib.loads(content.decode("utf-8"))
    if set(fields) != {"source", "database", "limits"}:
        raise ValueError("Configuration requires exactly source, database and limits tables.")
    source, database, limits = (fields[name] for name in ("source", "database", "limits"))
    if any(type(value) is not dict for value in (source, database, limits)):
        raise ValueError("Each configuration table must be a TOML object.")
    if set(source) != {"chain_id", "contract_address", "start_block", "rpc_url"}:
        raise ValueError("Source must specify chain_id, contract_address, start_block, rpc_url.")
    if set(database) != {"dsn_file", "runtime_role"} or type(database["dsn_file"]) is not str:
        raise ValueError("Database must specify dsn_file and runtime_role.")
    expected_limits = {
        "poll_seconds", "rpc_timeout_seconds", "max_response_bytes",
        "max_connections", "max_page_size", "max_evidence_blocks",
        "operation_timeout_seconds",
    }
    if set(limits) != expected_limits:
        raise ValueError(f"Limits must specify exactly {', '.join(sorted(expected_limits))}.")
    if type(source["contract_address"]) is not str or type(source["rpc_url"]) is not str:
        raise ValueError("Source contract_address and rpc_url must be strings.")
    return Settings(
        deployment=Deployment(source["chain_id"], source["contract_address"]),
        start_block=source["start_block"],
        rpc_url=source["rpc_url"],
        database_dsn_file=Path(database["dsn_file"]),
        runtime_role=database["runtime_role"],
        **limits,
    )


def read_dsn(path: Path) -> str:
    """Read an operator-owned secret file; never copy credentials into diagnostics."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Database credentials file must be a regular file with no group/other permissions.")
        if info.st_size <= 0 or info.st_size > 8192:
            raise ValueError("Database credentials file must contain 1..8192 bytes.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(8193)
    finally:
        os.close(fd)
    if len(raw) > 8192:
        raise ValueError("Database credentials file exceeds 8192 bytes.")
    value = raw.decode("utf-8").strip()
    if not value:
        raise ValueError("Database credentials file is empty.")
    return value


@asynccontextmanager
async def open_chain(settings: Settings):
    limits = httpx.Limits(
        max_connections=settings.max_connections,
        max_keepalive_connections=settings.max_connections,
    )
    async with httpx.AsyncClient(trust_env=False, timeout=None, limits=limits) as client:
        rpc = RpcClient(
            client, settings.rpc_url,
            timeout=settings.rpc_timeout_seconds,
            max_response_bytes=settings.max_response_bytes,
        )
        yield PayToPlay(rpc, settings.deployment)
