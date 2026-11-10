"""Install the new raw-history schema using the dedicated PostgreSQL owner.

This is a one-shot administrative operation. The scanner and readers never
receive this credential, execute DDL, or create a schema on startup.
"""

from uuid import uuid4

from psycopg import AsyncConnection

from conix.catalog import Catalog


class SchemaError(RuntimeError):
    """Schema ownership or existing state prevents safe initial installation."""


SCHEMA_DDL = (
    """
    CREATE TABLE conix.source_progress (
        singleton smallint PRIMARY KEY CHECK (singleton = 1),
        source_id bytea NOT NULL CHECK (octet_length(source_id) = 32),
        chain_id bigint NOT NULL CHECK (chain_id >= 0),
        genesis_hash bytea NOT NULL CHECK (octet_length(genesis_hash) = 32),
        history_start bigint NOT NULL CHECK (history_start >= 0),
        generation uuid NOT NULL,
        revision bigint NOT NULL CHECK (revision >= 0),
        last_block bigint,
        blocked_reason text,
        CHECK (last_block IS NULL OR last_block >= history_start)
    )
    """,
    """
    CREATE TABLE conix.blocks (
        number bigint PRIMARY KEY CHECK (number >= 0),
        hash bytea NOT NULL CHECK (octet_length(hash) = 32),
        parent_hash bytea NOT NULL CHECK (octet_length(parent_hash) = 32),
        timestamp bigint NOT NULL CHECK (timestamp >= 0)
    )
    """,
    """
    CREATE TABLE conix.events (
        block_number bigint NOT NULL
            REFERENCES conix.blocks (number) ON DELETE CASCADE,
        log_index bigint NOT NULL CHECK (log_index >= 0),
        transaction_hash bytea NOT NULL CHECK (octet_length(transaction_hash) = 32),
        transaction_index bigint NOT NULL CHECK (transaction_index >= 0),
        address bytea NOT NULL CHECK (octet_length(address) = 20),
        topic0 bytea NOT NULL CHECK (octet_length(topic0) = 32),
        topic1 bytea CHECK (topic1 IS NULL OR octet_length(topic1) = 32),
        topic2 bytea CHECK (topic2 IS NULL OR octet_length(topic2) = 32),
        topic3 bytea CHECK (topic3 IS NULL OR octet_length(topic3) = 32),
        data bytea NOT NULL,
        PRIMARY KEY (block_number, log_index),
        CHECK (topic2 IS NULL OR topic1 IS NOT NULL),
        CHECK (topic3 IS NULL OR topic2 IS NOT NULL)
    )
    """,
    """
    CREATE INDEX events_by_contract
        ON conix.events (address, topic0, block_number, transaction_index, log_index)
    """,
    """
    CREATE INDEX events_by_topic2
        ON conix.events (address, topic0, topic2, block_number, transaction_index, log_index)
    """,
    """
    CREATE INDEX events_by_topic3
        ON conix.events (address, topic0, topic3, block_number, transaction_index, log_index)
    """,
)


async def initialize(owner_dsn: str, catalog: Catalog) -> None:
    """Create empty tables and grants atomically; refuse upgrades or adoption."""
    async with await AsyncConnection.connect(
        owner_dsn,
        connect_timeout=5,
        autocommit=True,
    ) as connection:
        async with connection.transaction():
            # This bounds SQL work independently of any worker RPC deadline.
            await connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(catalog.limits.operation_timeout_seconds * 1000)),),
            )

            identity = await (
                await connection.execute(
                    "SELECT current_user, pg_get_userbyid(nspowner) "
                    "FROM pg_namespace WHERE nspname = 'conix'"
                )
            ).fetchone()

            if identity != ("conix_owner", "conix_owner"):
                raise SchemaError("Selected connection is not the owner of schema conix.")

            existing_tables = await (
                await connection.execute(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'conix'"
                )
            ).fetchone()

            if existing_tables[0] != 0:
                raise SchemaError("Schema conix already contains tables; initialization cannot replace history.")

            for statement in SCHEMA_DDL:
                await connection.execute(statement)

            await connection.execute(
                """
                INSERT INTO conix.source_progress (
                    singleton, source_id, chain_id, genesis_hash,
                    history_start, generation, revision
                )
                VALUES (1, %s, %s, %s, %s, %s, 0)
                """,
                (
                    catalog.source_id,
                    catalog.chain.chain_id,
                    catalog.chain.genesis_hash,
                    catalog.chain.start_block,
                    uuid4(),
                ),
            )

            # Grant exactly the journal's DML: progress is updated, blocks
            # are appended or removed on repair, and event rows are appended.
            # Deleting a block removes its events through the FK cascade.
            await connection.execute(
                "GRANT SELECT, UPDATE ON conix.source_progress TO conix_runtime"
            )
            await connection.execute(
                "GRANT SELECT, INSERT, DELETE ON conix.blocks TO conix_runtime"
            )
            await connection.execute(
                "GRANT SELECT, INSERT ON conix.events TO conix_runtime"
            )
            await connection.execute(
                "GRANT SELECT "
                "ON conix.source_progress, conix.blocks, conix.events TO conix_reader"
            )
