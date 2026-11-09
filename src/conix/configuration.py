"""Strict operator configuration; all work limits and identities are explicit."""

import os
import re
import stat
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from eth_utils import is_checksum_address

from conix.models import Deployment
from conix.pay_to_play import PayToPlay
from conix.rpc import RpcClient


@dataclass(frozen=True, slots=True)
class Settings:
    """Bind one deployment, storage identity and work limits without defaults."""

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

        # A valid source cannot override the independent work budgets.
        for (limit_name, maximum_seconds) in (
            ("poll_seconds", 3600),
            ("rpc_timeout_seconds", 60),
            ("operation_timeout_seconds", 300),
        ):
            configured_seconds = getattr(self, limit_name)

            if type(configured_seconds) not in (int, float) or not 0 < configured_seconds <= maximum_seconds:
                raise ValueError(f"Invalid {limit_name = }; expected a positive number no greater than {maximum_seconds} seconds.")

        for (limit_name, maximum_value) in (
            ("max_response_bytes", 4 * 1024 * 1024),
            ("max_connections", 32),
            ("max_page_size", 10000),
            ("max_evidence_blocks", 50000),
        ):
            configured_value = getattr(self, limit_name)

            if type(configured_value) is not int or not 1 <= configured_value <= maximum_value:
                raise ValueError(f"Invalid {limit_name = }; expected a positive integer no greater than {maximum_value}.")

        if not isinstance(self.database_dsn_file, Path) or not self.database_dsn_file.is_absolute():
            raise ValueError("Database dsn_file must be an absolute path.")

        if type(self.runtime_role) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.runtime_role) is None:
            raise ValueError("Database runtime_role must explicitly name a simple PostgreSQL login role.")

        rpc_endpoint = httpx.URL(self.rpc_url)

        if rpc_endpoint.scheme not in ("http", "https") or not rpc_endpoint.host or rpc_endpoint.userinfo or rpc_endpoint.fragment:
            raise ValueError("RPC URL must be HTTP(S), with no embedded credentials or fragment.")


def load_settings(path: Path) -> Settings:
    """Require exact source, database and limit settings; never infer endpoints."""
    config_bytes = path.read_bytes()

    if len(config_bytes) > 64 * 1024:
        raise ValueError("Configuration exceeds 64 KiB.")

    config_tables = tomllib.loads(config_bytes.decode("utf-8"))

    if set(config_tables) != {"source", "database", "limits"}:
        raise ValueError("Configuration requires exactly source, database and limits tables.")

    (source_values, database_values, limit_values) = (
        config_tables[name] for name in ("source", "database", "limits")
    )

    if any(type(values) is not dict for values in (source_values, database_values, limit_values)):
        raise ValueError("Each configuration table must be a TOML object.")

    if set(source_values) != {"chain_id", "contract_address", "start_block", "rpc_url"}:
        raise ValueError("Source must specify chain_id, contract_address, start_block, rpc_url.")

    if set(database_values) != {"dsn_file", "runtime_role"} or type(database_values["dsn_file"]) is not str:
        raise ValueError("Database must specify dsn_file and runtime_role.")

    expected_limit_names = {
        "poll_seconds", "rpc_timeout_seconds", "max_response_bytes",
        "max_connections", "max_page_size", "max_evidence_blocks",
        "operation_timeout_seconds",
    }

    if set(limit_values) != expected_limit_names:
        raise ValueError(f"Limits must specify exactly {', '.join(sorted(expected_limit_names))}.")

    if type(source_values["contract_address"]) is not str or type(source_values["rpc_url"]) is not str:
        raise ValueError("Source contract_address and rpc_url must be strings.")

    return Settings(
        deployment=Deployment(source_values["chain_id"], source_values["contract_address"]),
        start_block=source_values["start_block"],
        rpc_url=source_values["rpc_url"],
        database_dsn_file=Path(database_values["dsn_file"]),
        runtime_role=database_values["runtime_role"],
        **limit_values,
    )


def read_dsn(path: Path) -> str:
    """Read a private credential file once without following its final symlink."""
    credential_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))

    try:
        file_stat = os.fstat(credential_fd)

        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & 0o077:
            raise ValueError("Database credentials file must be a regular file with no group/other permissions.")

        if file_stat.st_size <= 0 or file_stat.st_size > 8192:
            raise ValueError("Database credentials file must contain 1..8192 bytes.")

        with os.fdopen(credential_fd, "rb", closefd=False) as stream:
            credential_bytes = stream.read(8193)
    finally:
        os.close(credential_fd)

    if len(credential_bytes) > 8192:
        raise ValueError("Database credentials file exceeds 8192 bytes.")

    database_dsn = credential_bytes.decode("utf-8").strip()

    if not database_dsn:
        raise ValueError("Database credentials file is empty.")

    return database_dsn


@asynccontextmanager
async def open_chain(settings: Settings):
    """Own the HTTP client lifetime, ignoring ambient host proxy settings."""
    http_limits = httpx.Limits(
        max_connections=settings.max_connections,
        max_keepalive_connections=settings.max_connections,
    )

    async with httpx.AsyncClient(trust_env=False, timeout=None, limits=http_limits) as http_client:
        rpc_client = RpcClient(
            client=http_client,
            url=settings.rpc_url,
            timeout=settings.rpc_timeout_seconds,
            max_response_bytes=settings.max_response_bytes,
        )

        yield PayToPlay(rpc_client, settings.deployment)
