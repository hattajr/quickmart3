import aiosqlite
from typing import AsyncGenerator
import os

DATABASE_PATH = os.getenv("DATABASE_PATH")

async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    conn = await aiosqlite.connect(DATABASE_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()