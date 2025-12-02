# /// script
# dependencies = [
#   "loguru",
# ]
# ///

import sqlite3
from pathlib import Path
import sys
import datetime
import os
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time} {level} {message}")

def create_migration_versioning_table(db_file_path):
    db_file_path = Path(db_file_path)

    sql_schema_migration = """
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        PRAGMA temp_store = MEMORY;
        PRAGMA cache_size = -20000;
        PRAGMA busy_timeout = 5000;

        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""
    
    conn = sqlite3.connect(db_file_path)
    with conn:
        conn.executescript(sql_schema_migration)
    logger.info("Migration versioning table created.")

def run_migrations(db_path: Path):
    if not db_path.exists():
        logger.error(f"Database file not found: {db_path}")
        raise Exception("Database file not found")
    
    migration_dir = Path("migrations")
    if not migration_dir.exists():
        logger.error(f"Migrations directory not found: {migration_dir}")
        raise Exception("Migrations directory not found")
    
    conn = sqlite3.connect(db_path)
    
    try:
        for sql_file in sorted(migration_dir.glob("*.sql")):
            filename = sql_file.name
            
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE filename = ?", (filename,)
            ).fetchone()
            if applied:
                continue
            
            with open(sql_file, 'r', encoding='utf-8') as f:
                sql = f.read()
            
            try:
                conn.execute("BEGIN")
                
                statements = [stmt.strip() for stmt in sql.split(';') if stmt.strip()]
                for statement in statements:
                    conn.execute(statement)
                
                conn.execute(
                    "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                    (filename, datetime.datetime.now(datetime.timezone.utc).isoformat(" ")),
                )
                
                conn.commit()
                logger.info(f"+{filename}")
                
            except sqlite3.Error as e:
                logger.error(f"Failed to apply migration {filename}: {e}")
                conn.rollback()
                continue
    finally:
        conn.close()

if __name__ == "__main__":
    db_path = os.getenv("DATABASE_PATH")
    if not db_path:
        logger.error("DATABASE_PATH environment variable not set")
        sys.exit(1)
    
    db_path = Path(db_path)
    if not db_path.exists():
        if not db_path.parent.exists():
            db_path.parent.mkdir(parents=True)
        create_migration_versioning_table(db_path)

    args = sys.argv[1:]
    if args and args[0] == "--rebuild":
        conn = sqlite3.connect(db_path)
        keep_table= "schema_migrations"
        with conn:
            tables = conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name != '{keep_table}';").fetchall()
            for table in tables:
                conn.execute(f"DROP TABLE IF EXISTS {table[0]};")
            conn.execute(f"DELETE FROM {keep_table};")
        logger.info("Reset database")

    run_migrations(db_path)
    logger.info("Table is up-to-date.")
