"""Run the app in dev mode with a disposable SQLite database."""

import atexit
import os
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from loguru import logger


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DevConfig:
    """Store dev-run configuration from environment variables."""

    sqlite_path: Path
    host: str
    port: int
    reload: bool
    reset_db_on_start: bool
    cleanup_db_on_exit: bool


def _load_config() -> DevConfig:
    """Load runtime configuration from environment variables."""
    sqlite_path = Path(os.getenv("SQLITE_PATH", "data/quickmart.dev.sqlite3")).expanduser().resolve()
    return DevConfig(
        sqlite_path=sqlite_path,
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "9982")),
        reload=_env_flag("RELOAD", True),
        reset_db_on_start=_env_flag("DEV_DB_RESET_ON_START", True),
        cleanup_db_on_exit=_env_flag("DEV_DB_CLEANUP_ON_EXIT", True),
    )


def _sqlite_sidecar_paths(sqlite_path: Path) -> list[Path]:
    """Return the SQLite database path plus WAL/SHM sidecars."""
    return [sqlite_path, Path(f"{sqlite_path}-wal"), Path(f"{sqlite_path}-shm")]


def _remove_sqlite_files(sqlite_path: Path) -> None:
    """Delete the SQLite database file and its sidecars if they exist."""
    for path in _sqlite_sidecar_paths(sqlite_path):
        if path.exists():
            path.unlink()
            logger.info(f"Removed dev database file: {path}")


def _prepare_database(config: DevConfig) -> None:
    """Create a clean dev database before the server starts."""
    os.environ["SQLITE_DIR"] = str(config.sqlite_path.parent)
    os.environ["SQLITE_PATH"] = str(config.sqlite_path)
    config.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    if config.reset_db_on_start:
        _remove_sqlite_files(config.sqlite_path)

    from app.db.database import init_db

    init_db()
    logger.info(f"Dev database ready at {config.sqlite_path}")


def _register_cleanup(config: DevConfig) -> None:
    """Register process-exit cleanup for the disposable dev database."""

    def cleanup() -> None:
        if not config.cleanup_db_on_exit:
            logger.info(f"Keeping dev database at {config.sqlite_path}")
            return
        _remove_sqlite_files(config.sqlite_path)

    atexit.register(cleanup)


def main() -> None:
    """Start the development server with disposable SQLite state."""
    config = _load_config()
    _prepare_database(config)
    _register_cleanup(config)

    logger.info(
        "Starting dev server on "
        f"{config.host}:{config.port} with SQLite at {config.sqlite_path} "
        f"(reload={config.reload}, reset_on_start={config.reset_db_on_start}, cleanup_on_exit={config.cleanup_db_on_exit})"
    )

    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=config.reload,
        workers=1,
    )


if __name__ == "__main__":
    main()


# Usage:
#   uv run --env-file .env.dev run_dev.py
#
# Default dev DB behavior:
#   DEV_DB_RESET_ON_START=1   -> delete the old dev DB before startup
#   DEV_DB_CLEANUP_ON_EXIT=1  -> delete the dev DB when the server stops
#
# Set either flag to 0 in .env.dev if you want to keep the database around.