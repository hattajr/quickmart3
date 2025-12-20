"""
FastAPI application entry point for QuickMart POS system.
"""
import sys
from pathlib import Path

# Add parent directory to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from app.config import APP_HOST, APP_PORT, LOG_LEVEL, RELOAD, SESSION_SECRET_KEY, WORKERS
from app.routes import admin as admin_routes
from app.routes import cart as cart_routes
from app.routes import main as main_routes
from app.routes import products as products_routes
import subprocess
from contextlib import asynccontextmanager


logger.remove()
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    enqueue=True,
    format="[<level>{level: <8}</level>] <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Context manager for FastAPI app. It will run all code before `yield`
    on app startup, and will run code after `yeld` on app shutdown.
    """

    try:
        subprocess.run([
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
            "--minify"
        ])
    except Exception as e:
        print(f"Error running tailwindcss: {e}")

    yield

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
        workers=WORKERS,
        reload=RELOAD,
    )
