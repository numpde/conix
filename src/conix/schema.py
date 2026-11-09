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

# These are the only DML grants available to the runtime login.
_TABLES = {
    "source_progress": "SELECT, UPDATE",
    "blocks": "SELECT, INSERT, DELETE",
    "collections": "SELECT, INSERT",
}


async def initialize(owner_dsn: str, settings: Settings):
    """Enroll or verify a source with a distinct, unprivileged schema owner.

    Owner DDL and the ordinary writer share one PostgreSQL session lock;
    an active worker prevents installation from changing its storage.
    """
    async with await AsyncConnection.connect(
        conninfo=owner_dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=10,
    ) as connection:
        statement_timeout_ms = max(1, int(settings.operation_timeout_seconds * 1000))
        await connection.execute("SELECT set_config('statement_timeout', %s, false)", (f"{statement_timeout_ms}ms",))
        await connection.execute("SELECT set_config('lock_timeout', %s, false)", (f"{min(statement_timeout_ms, 5000)}ms",))

        owner_role_row = await (await connection.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )).fetchone()

        if (
            owner_role_row is None or not owner_role_row["rolcanlogin"]
            or any(owner_role_row[name] for name in (
                "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
            ))
            or owner_role_row["rolname"] == settings.runtime_role
        ):
            raise ConfigurationError("Schema initialization requires a separate non-superuser schema-owner login.")

        schema_owner_row = await (await connection.execute(
            "SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace "
            "WHERE nspname = 'conix'"
        )).fetchone()

        if schema_owner_row is None or schema_owner_row["owner"] != owner_role_row["rolname"]:
            raise ConfigurationError("The conix schema must already exist and be owned by the selected schema-owner login.")

        runtime_role_row = await (await connection.execute(
            query="SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                  "rolreplication, rolbypassrls, "
                  "pg_has_role(%s, current_user, 'member') AS owner_member "
                  "FROM pg_roles WHERE rolname = %s",
            params=(settings.runtime_role, settings.runtime_role),
        )).fetchone()

        if (
            runtime_role_row is None or not runtime_role_row["rolcanlogin"] or runtime_role_row["owner_member"]
            or any(runtime_role_row[name] for name in (
                "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
            ))
        ):
            raise ConfigurationError("The runtime role must be a separate unprivileged login without schema-owner membership.")

        # Schema enrollment must not race with a source's active writer.
        advisory_lock_key = advisory_key(settings)
        lock_result = await (await connection.execute(
            query="SELECT pg_try_advisory_lock(%s) AS locked",
            params=(advisory_lock_key,),
        )).fetchone()

        if not lock_result["locked"]:
            raise WriterBusy("Cannot initialize schema while the selected Conix writer is active.")

        try:
            async with connection.transaction():
                for ddl_statement in SCHEMA.split(";"):
                    if ddl_statement.strip():
                        await connection.execute(ddl_statement)

                # Reinstallation verifies existing identity; it never replaces
                # a source progress row or resets an existing generation.
                await connection.execute(
                    query="INSERT INTO conix.source_progress "
                          "(id, schema_version, source_digest, chain_id, contract_address, "
                          "start_block, generation, revision, collection_count) "
                          "VALUES (1, %s, %s, %s, %s, %s, %s, 0, 0) "
                          "ON CONFLICT (id) DO NOTHING",
                    params=(
                        VERSION,
                        source_digest(settings),
                        settings.deployment.chain_id,
                        bytes.fromhex(settings.deployment.address[2:]),
                        settings.start_block,
                        uuid4(),
                    ),
                )

                table_owner_rows = await (await connection.execute(
                    "SELECT relname, pg_get_userbyid(relowner) AS owner "
                    "FROM pg_class WHERE relnamespace = 'conix'::regnamespace "
                    "AND relkind = 'r'"
                )).fetchall()
                table_owners = {row["relname"]: row["owner"] for row in table_owner_rows}

                if any(table_owners.get(table_name) != owner_role_row["rolname"] for table_name in _TABLES):
                    raise ConfigurationError("Conix data tables must exist and belong to the selected schema owner.")

                await _state(connection, settings)

                # The runtime login receives only DML grants, never schema
                # creation, table truncation or source-row deletion.
                runtime_role_identifier = sql.Identifier(settings.runtime_role)

                await connection.execute(
                    query=sql.SQL("REVOKE ALL ON SCHEMA conix FROM PUBLIC, {}").format(runtime_role_identifier),
                )
                await connection.execute(
                    query=sql.SQL("GRANT USAGE ON SCHEMA conix TO {}").format(runtime_role_identifier),
                )

                for (table_name, allowed_privileges) in _TABLES.items():
                    table_identifier = sql.Identifier("conix", table_name)

                    await connection.execute(
                        query=sql.SQL("REVOKE ALL ON TABLE {} FROM PUBLIC, {}").format(table_identifier, runtime_role_identifier),
                    )
                    await connection.execute(
                        query=sql.SQL("GRANT " + allowed_privileges + " ON TABLE {} TO {}").format(table_identifier, runtime_role_identifier),
                    )
        finally:
            if not connection.closed:
                await connection.execute(
                    query="SELECT pg_advisory_unlock(%s)",
                    params=(advisory_lock_key,),
                )
