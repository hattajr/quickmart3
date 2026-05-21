"""
Database connection and transaction management (SQLite).
"""

import sqlite3
from pathlib import Path
from typing import Generator

from loguru import logger

from app.config import SQLITE_DIR, SQLITE_PATH


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply required SQLite runtime settings on every new connection."""
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    # FULL favors durability over write throughput so committed sales survive
    # abrupt host/container restarts more reliably.
    conn.execute("PRAGMA synchronous = FULL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA temp_store = MEMORY;")


def init_db() -> None:
    """Apply the schema on every startup.

    All DDL uses IF NOT EXISTS and seed rows use INSERT OR IGNORE, so this is
    idempotent: new databases are fully initialised, existing ones receive any
    new tables, triggers, or seed rows added since they were last created.
    """
    Path(SQLITE_DIR).mkdir(parents=True, exist_ok=True)
    schema_path = Path(__file__).parent / "schema.sql"
    conn = sqlite3.connect(SQLITE_PATH)
    try:
        _apply_pragmas(conn)
        conn.executescript(schema_path.read_text())
        logger.info("Schema applied successfully")
    finally:
        conn.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Yield a SQLite connection with all required PRAGMAs applied."""
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        raise
    finally:
        conn.close()


def insert_transaction(
    session_id: str,
    item_id: int,
    item_name: str,
    price_at_purchase: float,
    quantity: int,
    total_price: float,
) -> None:
    """Insert a single sold item transaction."""
    for conn in get_db():
        conn.execute(
            """
            INSERT INTO sold_items
            (session_id, item_id, item_name, price_at_purchase, quantity, total_price)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, item_id, item_name, price_at_purchase, quantity, total_price),
        )
        conn.commit()
        logger.debug(f"Inserted transaction for item {item_name} (ID: {item_id})")


def insert_transactions_batch(session_id: str, items: list[dict]) -> None:
    """Batch insert all cart items in a single database transaction."""
    if not items:
        return

    values = [
        (
            session_id,
            item["id"],
            item["name"],
            item["price"],
            item["qty"],
            item["price"] * item["qty"],
        )
        for item in items
    ]

    for conn in get_db():
        conn.executemany(
            """
            INSERT INTO sold_items
            (session_id, item_id, item_name, price_at_purchase, quantity, total_price)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.commit()
        logger.debug(f"Batch inserted {len(items)} transactions for session {session_id}")


def insert_sold_session(
    session_id: str,
    ip_address: str | None,
    user_agent: str,
    device_type: str,
    browser: str,
    os: str,
    country: str | None,
) -> None:
    """Insert a sold session record, ignoring duplicates."""
    for conn in get_db():
        conn.execute(
            """
            INSERT OR IGNORE INTO sold_sessions
            (session_id, ip_address, user_agent, device_type, browser, os, country)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, ip_address, user_agent, device_type, browser, os, country),
        )
        conn.commit()
        logger.debug(f"Logged sold session {session_id}")
