"""
Application configuration and environment variables.
"""
import os


MINIO_URL: str = os.getenv("MINIO_URL", "")
MINIO_PRODUCT_IMAGE_URL: str = f"{MINIO_URL}/ikmimart_images"
MINIO_CAROUSEL_IMAGE_URL: str = f"{MINIO_URL}/ikmimart_carousel"

PG_HOST: str = os.getenv("PG_HOST", "localhost")
PG_PORT: str = os.getenv("PG_PORT", "5432")
PG_DATABASE: str = os.getenv("PG_DATABASE", "quickmart")
PG_USER: str = os.getenv("PG_USER", "postgres")
PG_PASSWORD: str = os.getenv("PG_PASSWORD", "password")

APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT: int = int(os.getenv("APP_PORT", "9982"))
WORKERS: int = int(os.getenv("WORKERS", "1"))
RELOAD: bool = bool(int(os.getenv("RELOAD", "1")))

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG")
SESSION_SECRET_KEY: str = os.getenv("SESSION_SECRET_KEY", "your-super-secret-key")

# Admin authentication - CHANGE THIS IN PRODUCTION!
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")
