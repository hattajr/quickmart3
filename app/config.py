"""
Application configuration and environment variables.
"""

import os


def _required_env(name: str) -> str:
    """Return a required environment variable without a public fallback."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set before starting the application")
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Supabase S3 Storage Configuration
AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")
S3_BUCKET_NAME: str = os.getenv("BUCKET_NAME", "ikmimart")
S3_PRODUCT_IMAGES_FOLDER: str = os.getenv("PRODUCT_IMAGES_FOLDER", "low_ikmimart_images")
S3_CAROUSEL_IMAGES_FOLDER: str = os.getenv("CAROUSEL_IMAGES_FOLDER", "carousel_images")
S3_REGION: str = os.getenv("S3_REGION", "ap-southeast-1")

# Extract project ID from S3_ENDPOINT_URL (e.g., knnnzxegkqnkywoeonrm.storage.supabase.co)
if S3_ENDPOINT_URL:
    project_id = S3_ENDPOINT_URL.split("://")[1].split(".")[0] if "://" in S3_ENDPOINT_URL else ""
    SUPABASE_STORAGE_PUBLIC_URL = f"https://{project_id}.supabase.co/storage/v1/object/public/{S3_BUCKET_NAME}"
    PRODUCT_IMAGE_BASE_URL = f"{SUPABASE_STORAGE_PUBLIC_URL}/{S3_PRODUCT_IMAGES_FOLDER}"
    CAROUSEL_IMAGE_BASE_URL = f"{SUPABASE_STORAGE_PUBLIC_URL}/{S3_CAROUSEL_IMAGES_FOLDER}"
else:
    SUPABASE_STORAGE_PUBLIC_URL = ""
    PRODUCT_IMAGE_BASE_URL = ""
    CAROUSEL_IMAGE_BASE_URL = ""

# Fallback image for missing product images
NO_IMAGE_URL: str = "https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930"

APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT: int = int(os.getenv("APP_PORT", "9982"))
WORKERS: int = int(os.getenv("WORKERS", "1"))
RELOAD: bool = _env_flag("RELOAD", False)

SQLITE_DIR: str = os.getenv("SQLITE_DIR", "data")
SQLITE_PATH: str = os.getenv("SQLITE_PATH", f"{SQLITE_DIR}/quickmart.sqlite3")

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
SESSION_SECRET_KEY: str = _required_env("SESSION_SECRET_KEY")
if len(SESSION_SECRET_KEY) < 32:
    raise RuntimeError("SESSION_SECRET_KEY must contain at least 32 characters")

ADMIN_PASSWORD: str = _required_env("ADMIN_PASSWORD")
SESSION_COOKIE_SECURE: bool = _env_flag("SESSION_COOKIE_SECURE")

# Payment and contact details are deployment configuration, not source code.
PAYMENT_BANK_NAME: str = os.getenv("PAYMENT_BANK_NAME", "").strip()
PAYMENT_ACCOUNT_NAME: str = os.getenv("PAYMENT_ACCOUNT_NAME", "").strip()
PAYMENT_ACCOUNT_NUMBER: str = os.getenv("PAYMENT_ACCOUNT_NUMBER", "").strip()
PAYMENT_CONFIRMATION_PHONE: str = os.getenv("PAYMENT_CONFIRMATION_PHONE", "").strip()
PAYMENT_FEEDBACK_PHONE: str = os.getenv("PAYMENT_FEEDBACK_PHONE", "").strip()

PAYMENT_TEMPLATE_CONTEXT = {
    "payment_bank_name": PAYMENT_BANK_NAME,
    "payment_account_name": PAYMENT_ACCOUNT_NAME,
    "payment_account_number": PAYMENT_ACCOUNT_NUMBER,
    "payment_confirmation_phone": PAYMENT_CONFIRMATION_PHONE,
    "payment_feedback_phone": PAYMENT_FEEDBACK_PHONE,
}

# Fuzzy search configuration
FUZZY_SEARCH_THRESHOLD: int = int(os.getenv("FUZZY_SEARCH_THRESHOLD", "60"))
