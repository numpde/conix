"""Explicit owner-only schema installation; never invoked by the Tier-0 worker.

The database operator first creates a dedicated, non-superuser owner login,
a separate runtime login, and a schema owned by the former. This module owns
only the DDL, initial source binding and exact grants. A future migration
expands this owner action, not the concrete scan, observer or repair methods.
"""

from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from conix.configuration import Settings
from conix.errors import ConfigurationError, WriterBusy
from conix.journal import VERSION, _state, advisory_key, source_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS conix.source_progress (
    id smallint PRIMARY KEY CHECK (id = 1),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    source_digest bytea NOT NULL CHECK (octet_length(source_digest) = 32),
    chain_id bigint NOT NULL CHECK (chain_id >= 0),
    contract_address bytea NOT NULL CHECK (octet_length(contract_address) = 20),
    start_block bigint NOT NULL CHECK (start_block >= 0),
    generation uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0),
    completed_height bigint,
    completed_hash bytea CHECK (completed_hash IS NULL OR octet_length(completed_hash) = 32),
    collection_count bigint NOT NULL CHECK (collection_count >= 0),
    head_height bigint,
    head_hash bytea CHECK (head_hash IS NULL OR octet_length(head_hash) = 32),
    head_parent_hash bytea CHECK (head_parent_hash IS NULL OR octet_length(head_parent_hash) = 32),
    head_timestamp bigint,
    head_observed_at timestamptz,
    blocked_reason text,
    CHECK ((completed_height IS NULL) = (completed_hash IS NULL)),
    CHECK ((head_height IS NULL) = (head_hash IS NULL)),
    CHECK ((head_height IS NULL) = (head_parent_hash IS NULL)),
    CHECK ((head_height IS NULL) = (head_timestamp IS NULL)),
    CHECK ((head_height IS NULL) = (head_observed_at IS NULL))
);
CREATE TABLE IF NOT EXISTS conix.blocks (
    number bigint PRIMARY KEY CHECK (number >= 0),
    hash bytea NOT NULL UNIQUE CHECK (octet_length(hash) = 32),
    parent_hash bytea NOT NULL CHECK (octet_length(parent_hash) = 32),
    timestamp bigint NOT NULL CHECK (timestamp >= 0)
);
CREATE TABLE IF NOT EXISTS conix.collections (
    sequence bigint PRIMARY KEY CHECK (sequence >= 0),
    block_number bigint NOT NULL REFERENCES conix.blocks(number) ON DELETE CASCADE,
    block_hash bytea NOT NULL CHECK (octet_length(block_hash) = 32),
    block_timestamp bigint NOT NULL CHECK (block_timestamp >= 0),
    transaction_hash bytea NOT NULL CHECK (octet_length(transaction_hash) = 32),
    transaction_index bigint NOT NULL CHECK (transaction_index >= 0),
    log_index bigint NOT NULL CHECK (log_index >= 0),
    customer bytea NOT NULL CHECK (octet_length(customer) = 20),
    channel_id numeric NOT NULL CHECK (channel_id >= 0 AND channel_id = trunc(channel_id)),
    payee bytea NOT NULL CHECK (octet_length(payee) = 20),
    amount numeric NOT NULL CHECK (amount > 0 AND amount = trunc(amount)),
    voucher_paid numeric NOT NULL CHECK (voucher_paid >= amount AND voucher_paid = trunc(voucher_paid)),
    invoice_hash bytea NOT NULL CHECK (octet_length(invoice_hash) = 32),
    UNIQUE (block_hash, log_index),
    UNIQUE (block_number, log_index)
);
CREATE INDEX IF NOT EXISTS collections_block_position ON conix.collections(block_number, log_index);
"""

# This is the exact runtime grant boundary, not a general schema default.
_TABLES = {
    "source_progress": "SELECT, UPDATE",
    "blocks": "SELECT, INSERT, DELETE",
    "collections": "SELECT, INSERT",
}


async def initialize(owner_dsn: str, settings: Settings):
    """Install/verify this source with a dedicated schema-owner connection.

    The owner may perform DDL; the runtime role is a distinct, non-owner login.
    Both owner installation and normal journal writing use the same PostgreSQL
    session advisory lock, so an active Tier-0 writer prevents schema changes.
    """
    async with await AsyncConnection.connect(
        owner_dsn, autocommit=True, row_factory=dict_row, connect_timeout=10
    ) as connection:
        timeout_ms = max(1, int(settings.operation_timeout_seconds * 1000))
        await connection.execute(
            "SELECT set_config('statement_timeout', %s, false)", (f"{timeout_ms}ms",)
        )
        await connection.execute(
            "SELECT set_config('lock_timeout', %s, false)", (f"{min(timeout_ms, 5000)}ms",)
        )
        role = await (await connection.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )).fetchone()
        if (
            role is None or not role["rolcanlogin"]
            or any(role[name] for name in (
                "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
            ))
            or role["rolname"] == settings.runtime_role
        ):
            raise ConfigurationError(
                "Schema initialization requires a separate non-superuser schema-owner login."
            )
        row = await (await connection.execute(
            "SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace "
            "WHERE nspname = 'conix'"
        )).fetchone()
        if row is None or row["owner"] != role["rolname"]:
            raise ConfigurationError(
                "The conix schema must already exist and be owned by the selected schema-owner login."
            )

        runtime = await (await connection.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls, "
            "pg_has_role(%s, current_user, 'member') AS owner_member "
            "FROM pg_roles WHERE rolname = %s",
            (settings.runtime_role, settings.runtime_role),
        )).fetchone()
        if (
            runtime is None or not runtime["rolcanlogin"] or runtime["owner_member"]
            or any(runtime[name] for name in (
                "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
            ))
        ):
            raise ConfigurationError(
                "The runtime role must be a separate unprivileged login without schema-owner membership."
            )
        # One lock excludes schema enrollment from a running writer without
        # exposing owner credentials to the runtime process.
        key = advisory_key(settings)
        locked = await (await connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (key,)
        )).fetchone()
        if not locked["locked"]:
            raise WriterBusy("Cannot initialize schema while the selected Conix writer is active.")

        try:
            async with connection.transaction():
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        await connection.execute(statement)
                # Repeated installation verifies rather than re-enrolling or
                # resetting a source with existing collection history.
                await connection.execute(
                    "INSERT INTO conix.source_progress "
                    "(id, schema_version, source_digest, chain_id, contract_address, "
                    "start_block, generation, revision, collection_count) "
                    "VALUES (1, %s, %s, %s, %s, %s, %s, 0, 0) "
                    "ON CONFLICT (id) DO NOTHING",
                    (VERSION, source_digest(settings), settings.deployment.chain_id,
                     bytes.fromhex(settings.deployment.address[2:]),
                     settings.start_block, uuid4()),
                )
                # Refuse pre-existing tables owned by another role instead of
                # adopting them, and refuse a mismatched source without a reset.
                rows = await (await connection.execute(
                    "SELECT relname, pg_get_userbyid(relowner) AS owner "
                    "FROM pg_class WHERE relnamespace = 'conix'::regnamespace "
                    "AND relkind = 'r'"
                )).fetchall()
                found = {record["relname"]: record["owner"] for record in rows}
                if any(found.get(table) != role["rolname"] for table in _TABLES):
                    raise ConfigurationError(
                        "Conix data tables must exist and belong to the selected schema owner."
                    )

                await _state(connection, settings)
                # Owner controls all privileges; runtime is deliberately given
                # no schema CREATE, table TRUNCATE or source-row INSERT/DELETE.
                target = sql.Identifier(settings.runtime_role)
                await connection.execute(
                    sql.SQL("REVOKE ALL ON SCHEMA conix FROM PUBLIC, {}").format(target)
                )
                await connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA conix TO {}").format(target)
                )
                for (table, allowed) in _TABLES.items():
                    name = sql.Identifier("conix", table)
                    await connection.execute(
                        sql.SQL("REVOKE ALL ON TABLE {} FROM PUBLIC, {}").format(name, target)
                    )
                    await connection.execute(
                        sql.SQL("GRANT " + allowed + " ON TABLE {} TO {}").format(name, target)
                    )
        finally:
            if not connection.closed:
                await connection.execute(
                    "SELECT pg_advisory_unlock(%s)", (key,)
                )
