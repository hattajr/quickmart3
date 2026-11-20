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
    "app.routes.admin_common",
    "app.routes.admin",
    "app.routes.admin_invoices",
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
    app.include_router(modules["app.routes.admin_invoices"].router)
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
    os.environ["SESSION_SECRET_KEY"] = "t" * 32
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

    search_response = harness.client.get("/admin/products/rows", params={"q": "mixed"})
    assert search_response.status_code == 200
    assert "MiXeD Tea" in search_response.text

    no_match = harness.client.get("/admin/products/rows", params={"q": "coffee"})
    assert "MiXeD Tea" not in no_match.text

    duplicate_response = harness.client.post(
        "/admin/products/add",
        data={
            "barcode": "0001",
            "name": "MiXeD Tea",
            "brand": "Brand",
            "price": 100,
            "unit": "pcs",
            "stock": 5,
            "description": "duplicate",
            "keyword": "tea",
            "image_url": "",
        },
    )
    assert duplicate_response.status_code == 200
    assert "A product with this name already exists." in duplicate_response.text
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM products") == 1


def test_admin_mutations_require_session_and_do_not_echo_password(harness: AppHarness) -> None:
    """Admin actions require the signed session created by login, not a form password."""
    _seed_product(harness.sqlite_path, 1, "Protected Milk", "dairy")

    unauthorized_response = harness.client.get(
        "/admin/products/rows", params={"password": "test-admin-password", "q": "milk"}
    )
    assert unauthorized_response.status_code == 401

    wrong_password = harness.client.post("/admin/verify", data={"password": "nope"})
    assert "That password is not right." in wrong_password.text

    login_response = harness.client.post("/admin/verify", data={"password": "test-admin-password"})
    assert login_response.status_code == 200
    assert 'name="password"' not in login_response.text

    authorized_response = harness.client.get("/admin/products/rows", params={"q": "milk"})
    assert authorized_response.status_code == 200
    assert "Protected Milk" in authorized_response.text


def test_admin_edit_delete_and_catalog_refresh(harness: AppHarness) -> None:
    """Admin edits should update timestamps and deletes should invalidate cached search data."""
    _seed_product(harness.sqlite_path, 1, "Old Milk", "dairy")
    login_response = harness.client.post("/admin/verify", data={"password": "test-admin-password"})
    assert login_response.status_code == 200

    harness.product_cache.refresh()
    assert harness.product_cache._invalidated is False

    edit_response = harness.client.post(
        "/admin/products/1/edit",
        data={
            "barcode": "0001",
            "name": "Updated Milk",
            "brand": "Brand",
            "price": 150,
            "unit": "pcs",
            "stock": 7,
            "description": "updated",
            "keyword": "fresh milk",
            "image_url": "",
        },
    )
    assert edit_response.status_code == 200
    assert 'id="product-1"' in edit_response.text
    assert "Saved Updated Milk" in edit_response.text

    updated_row = _query_one(
        harness.sqlite_path,
        "SELECT name, created_at, updated_at FROM products WHERE id = ?",
        (1,),
    )
    assert updated_row["name"] == "Updated Milk"
    assert updated_row["updated_at"] > updated_row["created_at"]

    delete_response = harness.client.delete("/admin/products/1")
    assert delete_response.status_code == 200
    assert 'hx-swap-oob="delete"' in delete_response.text
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM products") == 0
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


def _jpeg_bytes() -> bytes:
    """Return a small real JPEG."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _store_invoice(sqlite_path: Path) -> int:
    """Store a one-line 하나피아 invoice for Hydro Coco and return its id."""
    from app.services.supplier_invoices.ledger import build_invoices, store_invoice
    from app.services.supplier_invoices.parse import Item, Page

    items = [Item("하이드로코코", "Hydro Coco 330ml", "24 EA", "1", 1, 37008, 37008, "", False)]
    page = Page(
        "a.jpg", "sha-a", 0, _jpeg_bytes(), 40, 60, "{}", "하나피아", "거래명세표", "1", "2026-11-19", 1, 1, 1, 37008, items
    )
    invoices, failures = build_invoices([page])
    assert failures == []
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        return store_invoice(conn, invoices[0], "opus")


def _login(harness: AppHarness) -> None:
    response = harness.client.post("/admin/verify", data={"password": "test-admin-password"})
    assert response.status_code == 200


def test_invoice_photos_are_queued_only_for_the_admin(harness: AppHarness) -> None:
    """Sending photos needs a login, accepts real images, and rejects other files."""
    photo = ("photos", ("page-1.jpg", _jpeg_bytes(), "image/jpeg"))
    assert harness.client.post("/admin/invoices/uploads", files=[photo]).status_code == 401

    _login(harness)
    response = harness.client.post("/admin/invoices/uploads", files=[photo, photo])
    assert response.status_code == 200
    assert "Waiting to be read" in response.text
    assert _query_value(harness.sqlite_path, "SELECT COUNT(*) FROM supplier_invoice_upload_photos") == 2

    bad = harness.client.post("/admin/invoices/uploads", files=[("photos", ("x.jpg", b"not an image", "image/jpeg"))])
    assert bad.status_code == 400
    assert "not a photo" in bad.text

    page = harness.client.get("/admin/invoices")
    assert page.status_code == 200
    assert "Waiting to be read" in page.text


def test_upload_status_sends_the_page_to_the_invoice_when_read(harness: AppHarness) -> None:
    """A read upload's status call points the page at its invoice."""
    from app.services.supplier_invoices import uploads

    invoice_id = _store_invoice(harness.sqlite_path)
    with sqlite3.connect(harness.sqlite_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        upload_id = uploads.create_upload(conn, [_jpeg_bytes()])
        uploads.claim_next(conn)
        uploads.finish_upload(conn, upload_id, invoice_id, "")

    _login(harness)
    response = harness.client.get(f"/admin/invoices/uploads/{upload_id}")
    assert response.headers["HX-Redirect"] == f"/admin/invoices/{invoice_id}?upload={upload_id}"


def test_price_check_update_undo_and_match(harness: AppHarness) -> None:
    """The invoice page flags a low price; Update sets it, Undo restores it, and matches are saved."""
    _seed_product(harness.sqlite_path, 1, "Hydro coco original", "coconut")
    invoice_id = _store_invoice(harness.sqlite_path)
    assert harness.client.get(f"/admin/invoices/{invoice_id}/pages/1").status_code == 401

    _login(harness)
    page = harness.client.get(f"/admin/invoices/{invoice_id}")
    assert page.status_code == 200
    assert "Hydro Coco 330ml" in page.text
    assert "1 to fix" in page.text
    assert "1,800" in page.text

    item_id = _query_value(harness.sqlite_path, "SELECT id FROM supplier_invoice_items")
    updated = harness.client.post(f"/admin/invoices/items/{item_id}/update", data={"price": "1,800", "product_id": "1"})
    assert "Updated to ₩1,800" in updated.text
    assert "All done" in updated.text
    row = _query_one(harness.sqlite_path, "SELECT price, purchase_price FROM products WHERE id = 1")
    assert (row["price"], row["purchase_price"]) == (1800, 1542)

    undone = harness.client.post(f"/admin/invoices/items/{item_id}/undo")
    assert "Update price" in undone.text
    assert _query_value(harness.sqlite_path, "SELECT price FROM products WHERE id = 1") == 100

    sheet = harness.client.get(f"/admin/invoices/items/{item_id}/match")
    assert "Which shop product is this?" in sheet.text
    not_sold = harness.client.post(f"/admin/invoices/items/{item_id}/match", data={"product_id": ""})
    assert "Not sold in the shop" in not_sold.text
    assert _query_value(harness.sqlite_path, "SELECT product_id FROM supplier_product_matches") is None

    photo = harness.client.get(f"/admin/invoices/{invoice_id}/pages/1")
    assert photo.headers["content-type"] == "image/jpeg"
    assert harness.client.get(f"/admin/invoices/{invoice_id}/pages/9").status_code == 404