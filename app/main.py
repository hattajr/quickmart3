"""
FastAPI application entry point for QuickMart POS system.
"""

import sys
from pathlib import Path

# Add parent directory to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent))

import subprocess
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    APP_HOST,
    APP_PORT,
    LOG_LEVEL,
    RELOAD,
    SESSION_SECRET_KEY,
    SQLITE_DIR,
)
from app.db.database import get_db, init_db
from app.routes import admin as admin_routes
from app.routes import cart as cart_routes
from app.routes import main as main_routes
from app.routes import products as products_routes

logger.remove()
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    enqueue=True,
    format="[<level>{level: <8}</level>] <cyan>{elapsed}</cyan> <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Context manager for FastAPI app. It will run all code before `yield`
    on app startup, and will run code after `yeld` on app shutdown.
    """

    # Initialise SQLite database (creates schema if new)
    logger.info("Initialising SQLite database...")
    try:
        from pathlib import Path

        Path(SQLITE_DIR).mkdir(parents=True, exist_ok=True)
        init_db()
        for conn in get_db():
            conn.execute("SELECT 1")
        logger.info("Database connection successful")
    except Exception as e:
        logger.error(f"Database initialisation failed: {e}")
        raise

    # Compile Tailwind CSS
    logger.info("Compiling Tailwind CSS...")
    try:
        subprocess.run(
            [
                "uv",
                "tool",
                "run",
                "--from",
                "pytailwindcss",
                "tailwindcss",
                "-i",
                "app/static/css/input.css",
                "-o",
                "app/static/css/tailwind.css",
                "--minify",
            ],
            check=True,
        )
        logger.info("✓ Tailwind CSS compiled successfully")
    except Exception as e:
        logger.error(f"✗ Error running tailwindcss: {e}")

    yield

    logger.info("Application shutting down...")


app = FastAPI(lifespan=lifespan, title="QuickMart POS", version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(main_routes.router, tags=["main"])
app.include_router(cart_routes.router, tags=["cart"])
app.include_router(products_routes.router, tags=["products"])
app.include_router(admin_routes.router, tags=["admin"])


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=APP_HOST,
        port=APP_PORT,
        workers=1,
        reload=RELOAD,
    )
