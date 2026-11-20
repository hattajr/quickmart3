"""Admin login and the Products tab: search, add, edit and delete shop products."""

import logging
import sqlite3
from dataclasses import dataclass

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.routes.admin_common import (
    db_call,
    is_admin,
    parse_won,
    require_admin,
    safe_next,
    templates,
    verify_password,
)
from app.services.supplier_invoices.pricing import PRICE_WON_MAX
from app.utils.product_cache import product_cache

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter()

PRODUCT_LIST_MAX = 1000
TEXT_CHARS_MAX = 500
STOCK_MAX = 1_000_000
_COLUMNS = "id, barcode, name, brand, price, unit, stock, description, keyword, image_url"


@dataclass
class ProductForm:
    name: str
    brand: str | None
    price: int
    unit: str | None
    stock: int
    barcode: str | None
    description: str | None
    keyword: str | None
    image_url: str | None


def _blank(text: str | None) -> str | None:
    cleaned = (text or "").strip()
    return cleaned[:TEXT_CHARS_MAX] or None


def _read_form(
    name: str, brand: str, price: str, unit: str, stock: str, barcode: str, description: str, keyword: str, image: str
) -> ProductForm | str:
    """Check the product sheet's fields; return the form or what to fix."""
    clean_name = (name or "").strip()
    if not clean_name:
        return "Give the product a name."
    if len(clean_name) > TEXT_CHARS_MAX:
        return f"Keep the name under {TEXT_CHARS_MAX} characters."
    price_won = parse_won(price)
    if price_won is None or not 0 < price_won <= PRICE_WON_MAX:
        return f"Type a price from 1 to {PRICE_WON_MAX:,} won."
    stock_count = parse_won(stock) if (stock or "").strip() else 0
    if stock_count is None or stock_count > STOCK_MAX:
        return "Type the stock as a whole number."
    return ProductForm(
        name=clean_name,
        brand=_blank(brand),
        price=price_won,
        unit=_blank(unit),
        stock=stock_count,
        barcode=_blank(barcode),
        description=_blank(description),
        keyword=_blank(keyword),
        image_url=_blank(image),
    )


def _list_products(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    like = f"%{query.strip()}%"
    return conn.execute(
        f"SELECT {_COLUMNS} FROM products"
        " WHERE name LIKE ? OR coalesce(brand, '') LIKE ? OR coalesce(barcode, '') LIKE ? OR coalesce(keyword, '') LIKE ?"
        " ORDER BY name COLLATE NOCASE LIMIT ?",
        (like, like, like, like, PRODUCT_LIST_MAX),
    ).fetchall()


def _get_product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return conn.execute(f"SELECT {_COLUMNS} FROM products WHERE id = ?", (product_id,)).fetchone()


def _toast(message: str, kind: str = "ok") -> str:
    return templates.get_template("admin/_toast.html").render(message=message, type=kind)


def _sheet(request: Request, product: object | None, error: str = "", form: ProductForm | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "admin/_product_sheet.html", {"product": product, "error": error, "form": form}
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_home() -> Response:
    """Open the Products tab."""
    return RedirectResponse("/admin/products", status_code=303)


@router.get("/admin/products", response_class=HTMLResponse)
async def products_page(request: Request) -> Response:
    """Show the Products tab, or the login form when not logged in."""
    if not is_admin(request):
        return templates.TemplateResponse(request, "admin/login.html", {"next": "/admin/products", "error": ""})
    products = await db_call(lambda conn: _list_products(conn, ""))
    return templates.TemplateResponse(request, "admin/products.html", {"products": products, "tab": "products"})


@router.post("/admin/verify", response_class=HTMLResponse)
async def login(request: Request, password: str = Form(""), next: str = Form("/admin/products")) -> Response:
    """Log in with the admin password and go back to the page that asked for it."""
    if not verify_password(password):
        return templates.TemplateResponse(
            request, "admin/login.html", {"next": safe_next(next), "error": "That password is not right."}
        )
    request.session["admin_authenticated"] = True
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/admin/logout")
async def logout(request: Request) -> Response:
    """Log out of the admin."""
    request.session.pop("admin_authenticated", None)
    return RedirectResponse("/admin/products", status_code=303)


@router.get("/admin/products/rows", response_class=HTMLResponse)
async def product_rows(request: Request, q: str = "") -> Response:
    """Return the product rows matching a search."""
    require_admin(request)
    products = await db_call(lambda conn: _list_products(conn, q[:TEXT_CHARS_MAX]))
    return templates.TemplateResponse(request, "admin/_product_rows.html", {"products": products, "q": q})


@router.get("/admin/products/new", response_class=HTMLResponse)
async def new_product_sheet(request: Request) -> Response:
    """Return an empty product sheet."""
    require_admin(request)
    return _sheet(request, None)


@router.get("/admin/products/{product_id}/sheet", response_class=HTMLResponse)
async def product_sheet(request: Request, product_id: int) -> Response:
    """Return the edit sheet for one product."""
    require_admin(request)
    product = await db_call(lambda conn: _get_product(conn, product_id))
    if product is None:
        return HTMLResponse(_toast("That product no longer exists.", "error"))
    return _sheet(request, product)


@router.post("/admin/products/add", response_class=HTMLResponse)
async def add_product(
    request: Request,
    name: str = Form(""),
    brand: str = Form(""),
    price: str = Form(""),
    unit: str = Form(""),
    stock: str = Form(""),
    barcode: str = Form(""),
    description: str = Form(""),
    keyword: str = Form(""),
    image_url: str = Form(""),
) -> Response:
    """Add a product and refresh the list."""
    require_admin(request)
    form = _read_form(name, brand, price, unit, stock, barcode, description, keyword, image_url)
    if isinstance(form, str):
        return _sheet(request, None, form)

    def insert(conn: sqlite3.Connection) -> list[sqlite3.Row] | str:
        try:
            with conn:
                conn.execute(
                    "INSERT INTO products (barcode, name, brand, price, unit, stock, description, keyword, image_url)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (form.barcode, form.name, form.brand, form.price, form.unit, form.stock,
                     form.description, form.keyword, form.image_url),
                )  # fmt: skip
        except sqlite3.IntegrityError:
            return "A product with this name already exists."
        return _list_products(conn, "")

    result = await db_call(insert)
    if isinstance(result, str):
        return _sheet(request, None, result, form)
    product_cache.invalidate()
    logger.info("product added: %s", form.name)
    rows = templates.get_template("admin/_product_rows.html").render(products=result, q="", oob=True)
    return HTMLResponse(rows + _toast(f"Added {form.name}"))


@router.post("/admin/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product(
    request: Request,
    product_id: int,
    name: str = Form(""),
    brand: str = Form(""),
    price: str = Form(""),
    unit: str = Form(""),
    stock: str = Form(""),
    barcode: str = Form(""),
    description: str = Form(""),
    keyword: str = Form(""),
    image_url: str = Form(""),
) -> Response:
    """Save a product and refresh its row."""
    require_admin(request)
    form = _read_form(name, brand, price, unit, stock, barcode, description, keyword, image_url)
    if isinstance(form, str):
        product = await db_call(lambda conn: _get_product(conn, product_id))
        return _sheet(request, product, form)

    def update(conn: sqlite3.Connection) -> sqlite3.Row | str:
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE products SET barcode = ?, name = ?, brand = ?, price = ?, unit = ?, stock = ?,"
                    " description = ?, keyword = ?, image_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (form.barcode, form.name, form.brand, form.price, form.unit, form.stock,
                     form.description, form.keyword, form.image_url, product_id),
                )  # fmt: skip
        except sqlite3.IntegrityError:
            return "A product with this name already exists."
        if cursor.rowcount != 1:
            return "That product no longer exists."
        saved = _get_product(conn, product_id)
        return saved if saved is not None else "That product no longer exists."

    result = await db_call(update)
    if isinstance(result, str):
        product = await db_call(lambda conn: _get_product(conn, product_id))
        return _sheet(request, product, result, form)
    product_cache.invalidate()
    logger.info("product updated: %s (id %d)", form.name, product_id)
    row = templates.get_template("admin/_product_row.html").render(p=result, oob=True)
    return HTMLResponse(row + _toast(f"Saved {form.name}"))


@router.delete("/admin/products/{product_id}", response_class=HTMLResponse)
async def delete_product(request: Request, product_id: int) -> Response:
    """Delete a product that has never been sold."""
    require_admin(request)

    def delete(conn: sqlite3.Connection) -> str | None:
        try:
            with conn:
                cursor = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        except sqlite3.IntegrityError:
            return "This product has sales, so it can't be deleted. Set its stock to 0 instead."
        return None if cursor.rowcount == 1 else "That product no longer exists."

    problem = await db_call(delete)
    if problem is not None:
        product = await db_call(lambda conn: _get_product(conn, product_id))
        return _sheet(request, product, problem) if product is not None else HTMLResponse(_toast(problem, "error"))
    product_cache.invalidate()
    logger.info("product deleted: id %d", product_id)
    return HTMLResponse(f'<div id="product-{product_id}" hx-swap-oob="delete"></div>' + _toast("Product deleted"))
