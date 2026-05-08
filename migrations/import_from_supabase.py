# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "loguru>=0.7",
#   "psycopg2-binary>=2.9",
# ]
# ///
"""
One-shot migration: Supabase Postgres -> local SQLite.

Usage:
    uv run migrations/import_from_supabase.py

Note: the PEP 723 script block above means `uv run` resolves psycopg2-binary
automatically. Do NOT use `uv run python migrations/...` — that bypasses the
inline dependency block.

Environment variables required:
    PG_HOST, PG_PORT, PG_DATABASE, PG_USER, PG_PASSWORD  (source)
    SQLITE_PATH  (default: data/quickmart.sqlite3)
"""

import os
import sqlite3
import sys
from datetime import timezone

import psycopg2
import psycopg2.extras
from loguru import logger

BATCH_SIZE = 1000


def _load_env() -> dict[str, str]:
    """Read and validate all required environment variables.

    Checks every required key up front and exits non-zero with a precise message
    naming each missing variable, rather than raising a raw KeyError traceback.
    """
    required = ["PG_HOST", "PG_DATABASE", "PG_USER", "PG_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    return {
        "PG_HOST": os.environ["PG_HOST"],
        "PG_PORT": os.environ.get("PG_PORT", "5432"),
        "PG_DATABASE": os.environ["PG_DATABASE"],
        "PG_USER": os.environ["PG_USER"],
        "PG_PASSWORD": os.environ["PG_PASSWORD"],
        "SQLITE_PATH": os.environ.get("SQLITE_PATH", "data/quickmart.sqlite3"),
    }


# Table import order respects FK dependencies
TABLES: list[tuple[str, str, list[str]]] = [
    (
        "categories",
        """SELECT id, name, parent_id FROM categories ORDER BY id""",
        ["id", "name", "parent_id"],
    ),
    (
        "products",
        """SELECT id, barcode, name, brand, price, unit, stock, description,
                  category_id, keyword, image_url, created_at, updated_at,
                  purchase_price, latest_price
           FROM products ORDER BY id""",
        [
            "id",
            "barcode",
            "name",
            "brand",
            "price",
            "unit",
            "stock",
            "description",
            "category_id",
            "keyword",
            "image_url",
            "created_at",
            "updated_at",
            "purchase_price",
            "latest_price",
        ],
    ),
    (
        "sold_sessions",
        """SELECT id, session_id, ip_address::text, user_agent, device_type,
                  browser, os, country, created_at
           FROM sold_sessions ORDER BY id""",
        [
            "id",
            "session_id",
            "ip_address",
            "user_agent",
            "device_type",
            "browser",
            "os",
            "country",
            "created_at",
        ],
    ),
    (
        "sold_items",
        """SELECT id, session_id, item_id, item_name, price_at_purchase,
                  quantity, total_price, timestamp
           FROM sold_items ORDER BY id""",
        [
            "id",
            "session_id",
            "item_id",
            "item_name",
            "price_at_purchase",
            "quantity",
            "total_price",
            "timestamp",
        ],
    ),
    (
        "search_selections",
        """SELECT id, session_id, product_id, product_name, search_query,
                  ip_address::text, selected_at
           FROM search_selections ORDER BY id""",
        [
            "id",
            "session_id",
            "product_id",
            "product_name",
            "search_query",
            "ip_address",
            "selected_at",
        ],
    ),
    (
        "feedback_messages",
        """SELECT id, session_id, message, ip_address::text, user_agent,
                  created_at, is_deleted
           FROM feedback_messages ORDER BY id""",
        [
            "id",
            "session_id",
            "message",
            "ip_address",
            "user_agent",
            "created_at",
            "is_deleted",
        ],
    ),
]


def _normalise_value(v: object) -> object:
    """Convert Postgres-specific types to SQLite-compatible scalars.

    Timestamps are normalised to UTC and stored as 'YYYY-MM-DD HH:MM:SS',
    matching SQLite's CURRENT_TIMESTAMP format used throughout the app.
    """
    if v is None:
        return None
    from datetime import date, datetime

    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    return v


_IN_SCOPE_TABLES = [t[0] for t in TABLES]  # single source of truth for import-state checks

# PRAGMA user_version is set to this value only after a successful import completes.
# It is the sole canonical marker that distinguishes a finished import from a dirty database.
_IMPORT_COMPLETE_VERSION = 1


def _check_import_state(sqlite_conn: sqlite3.Connection) -> tuple[str, list[str]]:
    """Classify target database state before import.

    Uses PRAGMA user_version as the completion marker:
        0 (default) - import has never completed on this database
        1           - a previous import ran to completion

    Returns (state, populated_tables) where state is:
        'empty'   - user_version=0 and all tables empty; safe for first run
        'full'    - user_version=1; confirmed completed import, treat as rerun
        'partial' - user_version=0 but some tables already have rows; broken
                    prior state, abort so the operator can investigate
    """
    version = sqlite_conn.execute("PRAGMA user_version;").fetchone()[0]
    if version >= _IMPORT_COMPLETE_VERSION:
        return "full", []
    # No completion marker — inspect for partial data from an interrupted run
    populated = []
    for table in _IN_SCOPE_TABLES:
        row = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        if row and row[0] > 0:
            populated.append(table)
    if not populated:
        return "empty", populated
    return "partial", populated


def _import_table(
    pg_cursor: psycopg2.extras.RealDictCursor,
    sqlite_conn: sqlite3.Connection,
    table: str,
    query: str,
    columns: list[str],
) -> int:
    """Fetch all rows from Postgres, insert into SQLite, return row count."""
    pg_cursor.execute(query)
    placeholders = ",".join("?" * len(columns))
    col_names = ",".join(columns)
    insert_sql = f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})"

    total = 0
    while True:
        rows = pg_cursor.fetchmany(BATCH_SIZE)
        if not rows:
            break
        values = [tuple(_normalise_value(row[c]) for c in columns) for row in rows]
        sqlite_conn.executemany(insert_sql, values)
        total += len(rows)

    sqlite_conn.commit()
    return total


def _update_sequences(sqlite_conn: sqlite3.Connection, tables: list[str]) -> None:
    """Advance sqlite_sequence counters to MAX(id) so AUTOINCREMENT works."""
    for table in tables:
        row = sqlite_conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
        max_id = row[0] if row and row[0] is not None else 0
        exists = sqlite_conn.execute("SELECT 1 FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
        if exists:
            sqlite_conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                (max_id, table),
            )
        else:
            sqlite_conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (table, max_id),
            )
    sqlite_conn.commit()


def main() -> None:
    env = _load_env()
    logger.info("Starting Supabase -> SQLite migration")
    logger.info(f"Target: {env['SQLITE_PATH']}")

    pg_conn: psycopg2.extensions.connection | None = None
    sqlite_conn: sqlite3.Connection | None = None

    try:
        pg_conn = psycopg2.connect(
            host=env["PG_HOST"],
            port=env["PG_PORT"],
            database=env["PG_DATABASE"],
            user=env["PG_USER"],
            password=env["PG_PASSWORD"],
            cursor_factory=psycopg2.extras.RealDictCursor,
            connect_timeout=10,
        )
        sqlite_conn = sqlite3.connect(env["SQLITE_PATH"])
        state, populated_tables = _check_import_state(sqlite_conn)
        if state == "partial":
            logger.error(
                f"Partial import detected — tables already containing rows: {populated_tables}. "
                "Truncate all in-scope tables and re-run."
            )
            sys.exit(1)
        if state == "full":
            logger.warning(
                "All tables already contain data — treating this as a rerun. "
                "Existing rows will be skipped via INSERT OR IGNORE."
            )
        # Disable FK checks during bulk load; re-enable and verify after
        sqlite_conn.execute("PRAGMA foreign_keys = OFF;")
        sqlite_conn.execute("PRAGMA journal_mode = WAL;")
        sqlite_conn.execute("PRAGMA synchronous = NORMAL;")

        pg_cursor = pg_conn.cursor()
        table_names: list[str] = []
        for table, query, columns in TABLES:
            logger.info(f"Importing {table}...")
            count = _import_table(pg_cursor, sqlite_conn, table, query, columns)
            logger.info(f"  {table}: {count} rows")
            table_names.append(table)

        logger.info("Updating sqlite_sequence counters...")
        _update_sequences(sqlite_conn, table_names)

        logger.info("Final SQLite row counts:")
        for table in table_names:
            final_row = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
            logger.info(f"  {table}: {final_row[0] if final_row else 0} rows")

        # Re-enable FK checks and verify integrity
        sqlite_conn.execute("PRAGMA foreign_keys = ON;")
        orphans = sqlite_conn.execute("PRAGMA foreign_key_check;").fetchall()
        if orphans:
            for row in orphans:
                logger.warning(f"Orphaned FK row: {dict(zip(['table', 'rowid', 'parent', 'fkid'], row))}")
            logger.warning(f"{len(orphans)} orphaned FK rows found — review above")
        else:
            logger.info("Foreign key check passed — no orphans")

        # Stamp completion so future runs are identified as reruns, not partial states
        sqlite_conn.execute(f"PRAGMA user_version = {_IMPORT_COMPLETE_VERSION};")
        sqlite_conn.commit()
        logger.info("Migration complete")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        if sqlite_conn is not None:
            sqlite_conn.rollback()
        sys.exit(1)
    finally:
        if pg_conn is not None:
            pg_conn.close()
        if sqlite_conn is not None:
            sqlite_conn.close()


if __name__ == "__main__":
    main()
