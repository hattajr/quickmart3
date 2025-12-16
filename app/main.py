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
from app.routes import cart as cart_routes
from app.routes import main as main_routes
from app.routes import products as products_routes


logger.remove()
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    enqueue=True,
    format="[<level>{level: <8}</level>] <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

app: FastAPI = FastAPI(title="QuickMart POS", version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(main_routes.router, tags=["main"])
app.include_router(cart_routes.router, tags=["cart"])
app.include_router(products_routes.router, tags=["products"])


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=APP_HOST,
        port=APP_PORT,
        workers=WORKERS,
        reload=RELOAD,
    )
