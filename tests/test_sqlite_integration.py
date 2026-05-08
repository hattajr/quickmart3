"""Critical integration tests for the SQLite-backed app."""

import importlib
import os
import sqlite3
import sys
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_NAMES = [
    "app.config",
    "app.db.database",
    "app.utils",
    "app.utils.product_cache",
    "app.routes.admin",
    "app.routes.products",
    "app.routes.main",
]


@dataclass
class AppHarness:
    """Loaded modules, test app, and isolated SQLite path."""

    client: TestClient
    database: Any
    product_cache: Any
    sqlite_path: Path


def _reload_modules() -> dict[str, Any]:
    """Reload app modules after environment changes."""
    loaded: dict[str, Any] = {}
    for module_name in MODULE_NAMES:
        module = importlib.import_module(module_name)
        loaded[module_name] = importlib.reload(module)
    return loaded


def _build_test_app(secret_key: str, modules: dict[str, Any]) -> FastAPI:
    """Construct a minimal integration app using the project routers."""
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secret_key)
    app.mount("/static", StaticFiles(directory=str(REPO_ROOT / "app/static")), name="static")
    app.include_router(modules["app.routes.main"].router)
    app.include_router(modules["app.routes.products"].router)
    app.include_router(modules["app.routes.admin"].router)
    return app


def _seed_product(
    sqlite_path: Path,
    product_id: int,
    name: str,
    keyword: str,
    *,
    barcode: str | None = None,
    created_at: str = "2000-01-01 00:00:00",
    updated_at: str = "2000-01-01 00:00:00",
    image_url: str | None = None,
) -> None:
    """Insert a product row directly into the isolated test database."""
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO products (
                id, barcode, name, brand, price, unit, stock, description,
                category_id, keyword, image_url, created_at, updated_at,
                purchase_price, latest_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                barcode or f"{product_id:04d}",
                name,
                "Test Brand",
                100,
                "pcs",
                10,
                f"Description for {name}",
                None,
                keyword,
                image_url,
                created_at,
                updated_at,
                80,
                100,
            ),
        )
        conn.commit()


def _query_one(sqlite_path: Path, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    """Execute a query and return one row."""
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        assert row is not None
        return row


def _query_value(sqlite_path: Path, sql: str, params: tuple[object, ...] = ()) -> Any:
    """Execute a scalar query and return the first column."""
    return _query_one(sqlite_path, sql, params)[0]


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[AppHarness]:
    """Provide an isolated SQLite-backed test app."""
    sqlite_dir = tmp_path / "data"
    sqlite_dir.mkdir()
    sqlite_path = sqlite_dir / "quickmart.test.sqlite3"

    env_keys = ["SQLITE_DIR", "SQLITE_PATH", "SESSION_SECRET_KEY", "ADMIN_PASSWORD"]
    original_env = {key: os.environ.get(key) for key in env_keys}

    os.environ["SQLITE_DIR"] = str(sqlite_dir)
    os.environ["SQLITE_PATH"] = str(sqlite_path)
    os.environ["SESSION_SECRET_KEY"] = "test-session-secret"
    os.environ["ADMIN_PASSWORD"] = "test-admin-password"

    modules = _reload_modules()
    database = modules["app.db.database"]
    database.init_db()
    app = _build_test_app(modules["app.config"].SESSION_SECRET_KEY, modules)
    client = TestClient(app)

    try:
        yield AppHarness(
            client=client,
            database=database,
            product_cache=modules["app.utils.product_cache"].product_cache,
            sqlite_path=sqlite_path,
        )
    finally:
        client.close()
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_init_db_enables_pragmas_and_rejects_orphaned_sold_items(harness: AppHarness) -> None:
    """Cold start creates schema, enables WAL, and enforces foreign keys."""
    assert harness.sqlite_path.exists()

    for conn in harness.database.get_db():
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'").fetchone()[0] == "products"
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO sold_items (
                    session_id, item_id, item_name, price_at_purchase, quantity, total_price
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("session-1", 9999, "Missing Product", 10.0, 1, 10.0),
            )


def test_root_page_renders_and_initializes_a_session(harness: AppHarness) -> None:
    """The public home page should render successfully and establish a session."""
    response = harness.client.get("/")

    assert response.status_code == 200
    assert "session=" in response.headers.get("set-cookie", "")


def test_admin_verify_search_and_duplicate_name_behaviour(harness: AppHarness) -> None:
    """Admin login, case-insensitive search, and duplicate-name handling should work."""
    _seed_product(harness.sqlite_path, 1, "MiXeD Tea", "beverage")

    verify_response = harness.client.post("/admin/verify", data={"password": "test-admin-password"})
    assert verify_response.status_code == 200
    assert "MiXeD Tea" in verify_response.text

    search_response = harness.client.post(
        "/admin/products/search",
        data={
            "password": "test-admin-password",
            "q": "mixed",
            "sort_by": "id",
            "sort_order": "asc",
        },
    )
    assert search_response.status_code == 200
    assert "MiXeD Tea" in search_response.text

    duplicate_response = harness.client.post(
        "/admin/products/add",
        data={
            "password": "test-admin-password",
            "barcode": "0001",
            "name": "MiXeD Tea",
            "brand": "Brand",
            "price": 100,
            "unit": "pcs",
            "stock": 5,
            "description": "duplicate",
            "keyword": "tea",
            "image_url": "",
            "sort_by": "id",
            "sort_order": "asc",
        },
    )
    assert duplicate_response.status_code == 200
    assert "Product name already exists!" in duplicate_response.text


def test_admin_edit_delete_and_catalog_refresh(harness: AppHarness) -> None:
    """Admin edits should update timestamps and deletes should invalidate cached search data."""
    _seed_product(harness.sqlite_path, 1, "Old Milk", "dairy")

    harness.product_cache.refresh()
    assert harness.product_cache._invalidated is False

    edit_response = harness.client.post(
        "/admin/products/1/edit",
        data={
            "password": "test-admin-password",
            "barcode": "0001",
            "name": "Updated Milk",
            "brand": "Brand",
            "price": 150,
            "unit": "pcs",
            "stock": 7,
            "description": "updated",
            "keyword": "fresh milk",
            "image_url": "",
            "sort_by": "id",
            "sort_order": "asc",
        },
    )
    assert edit_response.status_code == 200

    updated_row = _query_one(
        harness.sqlite_path,
        "SELECT name, created_at, updated_at FROM products WHERE id = ?",
        (1,),
    )
    assert updated_row["name"] == "Updated Milk"
    assert updated_row["updated_at"] > updated_row["created_at"]

    delete_response = harness.client.delete(
        "/admin/products/1",
        params={"password": "test-admin-password"},
    )
    assert delete_response.status_code == 200
    assert delete_response.text == ""
    assert harness.product_cache._invalidated is True

    catalog_response = harness.client.get("/catalog")
    assert catalog_response.status_code == 200


def test_search_results_preserve_rank_order_and_log_selection(harness: AppHarness) -> None:
    """Search output should follow fuzzy rank order and log product selections."""
    _seed_product(harness.sqlite_path, 1, "Fresh Milk", "dairy")
    _seed_product(harness.sqlite_path, 2, "Milk Chocolate", "sweet")
    _seed_product(harness.sqlite_path, 3, "Oat Drink", "milk alternative")

    catalog_response = harness.client.get("/catalog")
    assert catalog_response.status_code == 200
    assert "Fresh Milk" in catalog_response.text

    ranked_ids = harness.product_cache.fuzzy_search("milk")
    assert ranked_ids

    search_response = harness.client.get("/search", params={"q": "milk"})
    assert search_response.status_code == 200

    positions = [search_response.text.index(f"search-item-{product_id}") for product_id in ranked_ids]
    assert positions == sorted(positions)

    log_response = harness.client.post(
        "/api/log-search-selection",
        json={
            "product_id": ranked_ids[0],
            "product_name": "Fresh Milk",
            "search_query": "milk",
        },
    )
    assert log_response.status_code == 200
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM search_selections") == 1


def test_search_results_escape_product_names(harness: AppHarness) -> None:
    """Search HTML should escape product names before rendering them into the page."""
    _seed_product(harness.sqlite_path, 1, '<img src=x onerror="alert(1)">', "milk")

    search_response = harness.client.get("/search", params={"q": "milk"})

    assert search_response.status_code == 200
    assert '<img src=x onerror="alert(1)">' not in search_response.text
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in search_response.text


def test_product_cache_detects_direct_sqlite_edits_with_delimiter_ambiguous_text(harness: AppHarness) -> None:
    """Direct DB edits should invalidate the cache even when names reshuffle delimiter text."""
    _seed_product(harness.sqlite_path, 1, "a|2:b", "")
    _seed_product(harness.sqlite_path, 2, "c", "")

    harness.product_cache.refresh()
    assert harness.product_cache.is_stale() is False

    # Simulate sqlite-web edits that change searchable text without touching updated_at.
    # These two row sets serialize to the same naive "id:text|id:text" string, so a
    # delimiter-unsafe fingerprint will miss the change.
    with sqlite3.connect(harness.sqlite_path) as conn:
        conn.execute("UPDATE products SET name = ?, keyword = ? WHERE id = ?", ("a", "", 1))
        conn.execute("UPDATE products SET name = ?, keyword = ? WHERE id = ?", ("b|2:c", "", 2))
        conn.commit()

    assert _query_value(harness.sqlite_path, "SELECT name FROM products WHERE id = ?", (1,)) == "a"
    assert _query_value(harness.sqlite_path, "SELECT name FROM products WHERE id = ?", (2,)) == "b|2:c"
    assert harness.product_cache.is_stale() is True


def test_search_fails_closed_when_sqlite_becomes_unreadable_after_cache_warmup(harness: AppHarness) -> None:
    """Search should return an empty result instead of a server error after DB loss."""
    _seed_product(harness.sqlite_path, 1, "Fresh Milk", "dairy")

    assert harness.product_cache.refresh() is True

    for suffix in ("", "-wal", "-shm"):
        db_path = Path(f"{harness.sqlite_path}{suffix}")
        if db_path.exists():
            db_path.unlink()

    with TestClient(harness.client.app, raise_server_exceptions=False) as no_raise_client:
        response = no_raise_client.get("/search", params={"q": "milk"})

    assert response.status_code == 200
    assert "No results found" in response.text


def test_checkout_and_feedback_persist_expected_rows(harness: AppHarness) -> None:
    """Checkout should batch sold items, dedupe sold sessions, and persist feedback."""
    _seed_product(harness.sqlite_path, 1, "Fresh Milk", "dairy")
    _seed_product(harness.sqlite_path, 2, "Bread", "bakery")

    payload = {
        "items": {
            "1": {"id": 1, "name": "Fresh Milk", "price": 12.5, "qty": 2},
            "2": {"id": 2, "name": "Bread", "price": 5.0, "qty": 1},
        }
    }

    first_checkout = harness.client.post("/finish", json=payload, headers={"user-agent": "pytest-agent"})
    second_checkout = harness.client.post("/finish", json=payload, headers={"user-agent": "pytest-agent"})

    assert first_checkout.status_code == 200
    assert second_checkout.status_code == 200
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM sold_items") == 4
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM sold_sessions") == 1

    feedback_response = harness.client.post("/feedback", data={"feedback_text": "Works well"})
    assert feedback_response.status_code == 200
    assert "Terima kasih" in feedback_response.text

    feedback_row = _query_one(
        harness.sqlite_path,
        "SELECT message, is_deleted FROM feedback_messages ORDER BY id DESC LIMIT 1",
    )
    assert feedback_row["message"] == "Works well"
    assert feedback_row["is_deleted"] == "N"


def test_checkout_rejects_carts_with_missing_products(harness: AppHarness) -> None:
    """Checkout should fail fast when the cart references a product that no longer exists."""
    payload = {
        "items": {
            "999": {"id": 999, "name": "Ghost Product", "price": 9.0, "qty": 1},
        }
    }

    response = harness.client.post("/finish", json=payload, headers={"user-agent": "pytest-agent"})

    assert response.status_code == 400
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM sold_items") == 0
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM sold_sessions") == 0


def test_template_routes_do_not_emit_deprecation_warnings(harness: AppHarness) -> None:
    """Template-rendering routes should not rely on deprecated Starlette call signatures."""
    _seed_product(harness.sqlite_path, 1, "Warn Test Product", "warning")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        verify_response = harness.client.post("/admin/verify", data={"password": "test-admin-password"})
        catalog_response = harness.client.get("/catalog")

    assert verify_response.status_code == 200
    assert catalog_response.status_code == 200

    deprecations = [warning for warning in caught if issubclass(warning.category, DeprecationWarning)]
    assert not deprecations