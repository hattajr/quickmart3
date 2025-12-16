"""
FastAPI application entry point for QuickMart POS system.
"""
import sys
import uvicorn
from loguru import logger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import LOG_LEVEL, SESSION_SECRET_KEY, APP_HOST, APP_PORT, WORKERS, RELOAD
from app.routes import main as main_routes
from app.routes import cart as cart_routes
from app.routes import products as products_routes


logger.remove()
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    enqueue=True,
    format="[<level>{level: <8}</level>] <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

app = FastAPI(title="QuickMart POS", version="1.0.0")
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
