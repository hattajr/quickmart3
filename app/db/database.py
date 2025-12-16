"""
Database connection and transaction management.
"""
import os
from typing import Generator
import psycopg2
from psycopg2.extras import RealDictCursor
from loguru import logger

from app.config import PG_HOST, PG_PORT, PG_DATABASE, PG_USER, PG_PASSWORD


def get_pg_db() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Create and yield a PostgreSQL database connection.
    """
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        database=PG_DATABASE,
        user=PG_USER,
        password=PG_PASSWORD,
        cursor_factory=RealDictCursor
    )
    try:
        yield conn
    finally:
        conn.close()


def insert_transaction(
    session_id: str,
    item_id: int,
    item_name: str,
    price_at_purchase: float,
    quantity: int,
    total_price: float
) -> None:
    """
    Insert a sold item transaction into the database.
    """
    for db in get_pg_db():
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO sold_items 
            (session_id, item_id, item_name, price_at_purchase, quantity, total_price) 
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (session_id, item_id, item_name, price_at_purchase, quantity, total_price)
        )
        db.commit()
        logger.debug(f"Inserted transaction for item {item_name} (ID: {item_id})")


def insert_sold_session(
    session_id: str,
    ip_address: str,
    user_agent: str,
    device_type: str,
    browser: str,
    os: str,
    country: str
) -> None:
    """
    Insert or update a sold session record in the database.
    """
    for db in get_pg_db():
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO sold_sessions 
            (session_id, ip_address, user_agent, device_type, browser, os, country) 
            VALUES (%s, %s, %s, %s, %s, %s, %s) 
            ON CONFLICT (session_id) DO NOTHING
            """,
            (session_id, ip_address, user_agent, device_type, browser, os, country)
        )
        db.commit()
        logger.debug(f"Logged sold session {session_id}")