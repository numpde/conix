"""Independent read-only PostgreSQL evidence for retained deployment tests.

This test-owned observer reads durable schema facts that the public Reader
does not expose as raw rows (ancestry, blocked state, generation, and exact
complete raw event records). It is not an alternate application reader.
"""

import psycopg


def snapshot(dsn):
    """Read one durable journal snapshot without write or owner privileges."""
    with psycopg.connect(
        dsn.strip(), connect_timeout=5, options="-c default_transaction_read_only=on",
    ) as db:
        # Progress and its rows must come from one durable state while the
        # installed scanner can commit new blocks between these SELECTs.
        db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        row = db.execute(
            "SELECT last_block,revision,generation::text,encode(source_id,'hex'),blocked_reason "
            "FROM conix.source_progress WHERE singleton=1"
        ).fetchone()
        blocks = db.execute(
            "SELECT number,encode(hash,'hex'),encode(parent_hash,'hex'),timestamp "
            "FROM conix.blocks ORDER BY number"
        ).fetchall()
        events = db.execute(
            "SELECT block_number,encode(transaction_hash,'hex'),log_index,transaction_index,"
            "encode(address,'hex'),encode(topic0,'hex'),encode(topic1,'hex'),"
            "encode(topic2,'hex'),encode(topic3,'hex'),encode(data,'hex') "
            "FROM conix.events ORDER BY block_number,log_index"
        ).fetchall()
    return {
        "height": row[0], "revision": row[1], "generation": row[2],
        "source_id": row[3], "blocked": row[4], "blocks": blocks,
        "events": events,
    }
