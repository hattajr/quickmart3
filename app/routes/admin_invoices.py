"""Admin Invoices tab: send invoice photos, follow their reading, and act on price suggestions."""

import asyncio
import io
import logging
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from PIL import Image, UnidentifiedImageError

from app.config import SQLITE_PATH
from app.routes.admin_common import db_call, is_admin, parse_won, require_admin, templates
from app.services.supplier_invoices import pricing, uploads
from app.services.supplier_invoices.pricing import PriceLine, ReviewError
from app.services.supplier_invoices.reader_status import ReaderStatus, read_status, status_path
from app.utils.product_cache import product_cache

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter()

RECENT_INVOICES_MAX = 20
SEARCH_QUERY_CHARS_MAX = 100


async def _reader() -> ReaderStatus:
    """Return whether the invoice worker runs and what stops it, from its heartbeat file."""
    return await asyncio.to_thread(read_status, status_path(Path(SQLITE_PATH)), time.time())


def _toast(message: str, kind: str = "ok") -> str:
    return templates.get_template("admin/_toast.html").render(message=message, type=kind)


def _card(line: PriceLine, extra: str = "") -> HTMLResponse:
    html = templates.get_template("admin/_price_card.html").render(line=line)
    return HTMLResponse(html + extra)


def _fix_count(count: int) -> str:
    return templates.get_template("admin/_fix_count.html").render(count=count, oob=True)


def _card_with_count(conn: sqlite3.Connection, item_id: int) -> tuple[PriceLine | None, int]:
    invoice_id = pricing.invoice_id_of_item(conn, item_id)
    review = None if invoice_id is None else pricing.build_review(conn, invoice_id)
    if review is None:
        return None, 0
    line = next((line for line in review.lines if line.item_id == item_id), None)
    return line, review.to_fix_count


async def _upload_card(upload: uploads.Upload) -> str:
    reader = await _reader()
    return templates.get_template("admin/_upload_card.html").render(u=upload, reader=reader)


@router.get("/admin/invoices", response_class=HTMLResponse)
async def invoices_page(request: Request) -> Response:
    """Show the Invoices tab: send photos, uploads in progress, and recent invoices."""
    if not is_admin(request):
        return templates.TemplateResponse(request, "admin/login.html", {"next": "/admin/invoices", "error": ""})

    def load(conn: sqlite3.Connection) -> tuple[list[uploads.Upload], list[pricing.InvoiceReview]]:
        return uploads.open_uploads(conn), pricing.recent_invoices(conn, RECENT_INVOICES_MAX)

    open_uploads, recent = await db_call(load)
    return templates.TemplateResponse(
        request,
        "admin/invoices.html",
        {
            "tab": "invoices",
            "uploads": open_uploads,
            "recent": recent,
            "reader": await _reader(),
            "photos_max": uploads.PHOTOS_PER_UPLOAD_MAX,
        },
    )


def _check_photo(data: bytes) -> str | None:
    """Return what is wrong with an uploaded photo, or None when it is a readable image."""
    if not data:
        return "One of the photos is empty."
    if len(data) > uploads.PHOTO_BYTES_MAX:
        return f"Each photo must be under {uploads.PHOTO_BYTES_MAX // (1024 * 1024)} MB."
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError):
        return "One of the files is not a photo this app can read. Take it again with the camera."
    return None


@router.post("/admin/invoices/uploads", response_class=HTMLResponse)
async def send_photos(request: Request, photos: list[UploadFile] = File(...)) -> Response:
    """Queue one invoice's photos for the worker and return its status card."""
    require_admin(request)
    if not 1 <= len(photos) <= uploads.PHOTOS_PER_UPLOAD_MAX:
        return HTMLResponse(f"Send 1 to {uploads.PHOTOS_PER_UPLOAD_MAX} photos at a time.", status_code=400)
    images: list[bytes] = []
    for photo in photos:
        data = await photo.read(uploads.PHOTO_BYTES_MAX + 1)
        problem = _check_photo(data)
        if problem is not None:
            return HTMLResponse(problem, status_code=400)
        images.append(data)

    def queue(conn: sqlite3.Connection) -> uploads.Upload:
        upload = uploads.get_upload(conn, uploads.create_upload(conn, images))
        if upload is None:
            raise RuntimeError("upload missing right after it was created")
        return upload

    upload = await db_call(queue)
    logger.info("queued invoice upload %d with %d photo(s)", upload.id, len(images))
    return HTMLResponse(await _upload_card(upload))


@router.get("/admin/invoices/uploads/{upload_id}", response_class=HTMLResponse)
async def upload_status(request: Request, upload_id: int) -> Response:
    """Return an upload's status card; once read, send the page to the invoice."""
    require_admin(request)
    upload = await db_call(lambda conn: uploads.get_upload(conn, upload_id))
    if upload is None:
        return HTMLResponse("")
    if upload.status == "done" and upload.invoice_id is not None:
        target = f"/admin/invoices/{upload.invoice_id}?upload={upload.id}"
        return HTMLResponse("", headers={"HX-Redirect": target})
    return HTMLResponse(await _upload_card(upload))


@router.post("/admin/invoices/uploads/{upload_id}/retry", response_class=HTMLResponse)
async def retry_upload(request: Request, upload_id: int) -> Response:
    """Send a failed upload back to the worker."""
    require_admin(request)

    def retry(conn: sqlite3.Connection) -> tuple[bool, uploads.Upload | None]:
        return uploads.retry_upload(conn, upload_id), uploads.get_upload(conn, upload_id)

    retried, upload = await db_call(retry)
    if upload is None:
        return HTMLResponse(_toast("That upload no longer exists.", "error"))
    if not retried:
        return HTMLResponse(
            await _upload_card(upload) + _toast("Send the photos again; this upload can't be retried.", "error")
        )
    return HTMLResponse(await _upload_card(upload))


@router.get("/admin/invoices/reader", response_class=HTMLResponse)
async def reader_banner(request: Request) -> Response:
    """Return the banner that says when the invoice reader is stopped or can't use Claude."""
    require_admin(request)
    reader = await _reader()
    return HTMLResponse(templates.get_template("admin/_reader_status.html").render(reader=reader))


@router.post("/admin/invoices/uploads/{upload_id}/delete", response_class=HTMLResponse)
async def delete_upload(request: Request, upload_id: int) -> Response:
    """Delete an upload that is not being read."""
    require_admin(request)
    deleted = await db_call(lambda conn: uploads.delete_upload(conn, upload_id))
    if not deleted:
        return HTMLResponse(_toast("It is being read right now, so it can't be deleted.", "error"))
    return HTMLResponse(_toast("Upload deleted"))


@router.get("/admin/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_page(request: Request, invoice_id: int, upload: int | None = None) -> Response:
    """Show one invoice with a price check for each product on it."""
    if not is_admin(request):
        return templates.TemplateResponse(
            request, "admin/login.html", {"next": f"/admin/invoices/{invoice_id}", "error": ""}
        )

    def load(conn: sqlite3.Connection) -> tuple[pricing.InvoiceReview | None, list[int], uploads.Upload | None]:
        sent = uploads.get_upload(conn, upload) if upload is not None else None
        return pricing.build_review(conn, invoice_id), pricing.page_numbers(conn, invoice_id), sent

    review, pages, sent = await db_call(load)
    if review is None:
        return HTMLResponse("That invoice does not exist.", status_code=404)
    note = sent.message if sent is not None and sent.invoice_id == invoice_id else ""
    return templates.TemplateResponse(
        request, "admin/invoice.html", {"tab": "invoices", "review": review, "pages": pages, "note": note}
    )


@router.get("/admin/invoices/{invoice_id}/photos", response_class=HTMLResponse)
async def invoice_photos(request: Request, invoice_id: int) -> Response:
    """Return a sheet with the invoice's photos."""
    require_admin(request)
    pages = await db_call(lambda conn: pricing.page_numbers(conn, invoice_id))
    return templates.TemplateResponse(request, "admin/_photos_sheet.html", {"invoice_id": invoice_id, "pages": pages})


@router.get("/admin/invoices/{invoice_id}/pages/{page_no}")
async def invoice_page_image(request: Request, invoice_id: int, page_no: int) -> Response:
    """Return one stored invoice photo; they show bank details, so only the admin may see them."""
    require_admin(request)
    image = await db_call(lambda conn: pricing.page_image(conn, invoice_id, page_no))
    if image is None:
        return Response(status_code=404)
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def _decide(item_id: int, action: str, product_id: int | None, price: str) -> Response:
    """Apply update, skip or undo to one line and return its card with the new count."""

    def work(conn: sqlite3.Connection) -> tuple[PriceLine | None, int, str]:
        try:
            if action == "update":
                price_won = parse_won(price)
                if product_id is None or price_won is None:
                    raise ReviewError("Type a price first.")
                pricing.apply_update(conn, item_id, product_id, price_won)
            elif action == "skip":
                pricing.apply_skip(conn, item_id)
            elif action == "undo":
                pricing.undo_review(conn, item_id)
            else:
                raise ValueError(f"unknown action {action!r}")
            problem = ""
        except ReviewError as error:
            problem = str(error)
        line, count = _card_with_count(conn, item_id)
        return line, count, problem

    line, count, problem = await db_call(work)
    if line is None:
        return HTMLResponse(_toast("This invoice line no longer exists.", "error"))
    if problem:
        return _card(line, _toast(problem, "error") + _fix_count(count))
    if action in ("update", "undo"):
        product_cache.invalidate()
    message = {"update": "Price updated", "skip": "Skipped", "undo": "Undone"}[action]
    if action == "update" and line.review is not None and line.shop is not None:
        message = f"{line.shop.label}: ₩{line.review.new_price:,}"
    return _card(line, _toast(message) + _fix_count(count))


@router.post("/admin/invoices/items/{item_id}/update", response_class=HTMLResponse)
async def update_price(
    request: Request, item_id: int, price: str = Form(""), product_id: int | None = Form(None)
) -> Response:
    """Set the shop price from an invoice line."""
    require_admin(request)
    return await _decide(item_id, "update", product_id, price)


@router.post("/admin/invoices/items/{item_id}/skip", response_class=HTMLResponse)
async def skip_price(request: Request, item_id: int) -> Response:
    """Leave the shop price of an invoice line as it is."""
    require_admin(request)
    return await _decide(item_id, "skip", None, "")


@router.post("/admin/invoices/items/{item_id}/undo", response_class=HTMLResponse)
async def undo_price(request: Request, item_id: int) -> Response:
    """Undo an update or skip."""
    require_admin(request)
    return await _decide(item_id, "undo", None, "")


@router.get("/admin/invoices/items/{item_id}/match", response_class=HTMLResponse)
async def match_sheet(request: Request, item_id: int) -> Response:
    """Return the sheet for picking which shop product an invoice line is."""
    require_admin(request)

    def load(conn: sqlite3.Connection) -> tuple[PriceLine | None, list[tuple[pricing.ShopProduct, float]]]:
        line = pricing.price_line(conn, item_id)
        if line is None:
            return None, []
        return line, pricing.rank_products(line.name_latin, pricing.load_shop(conn), pricing.MATCH_CANDIDATES_MAX)

    line, candidates = await db_call(load)
    if line is None:
        return HTMLResponse(_toast("This invoice line no longer exists.", "error"))
    return templates.TemplateResponse(request, "admin/_match_sheet.html", {"line": line, "candidates": candidates})


@router.get("/admin/invoices/items/{item_id}/match/search", response_class=HTMLResponse)
async def match_search(request: Request, item_id: int, q: str = "") -> Response:
    """Return shop products matching a typed search for the match sheet."""
    require_admin(request)
    query = q.strip()[:SEARCH_QUERY_CHARS_MAX]

    def load(conn: sqlite3.Connection) -> tuple[PriceLine | None, list[tuple[pricing.ShopProduct, float]]]:
        line = pricing.price_line(conn, item_id)
        if line is None:
            return None, []
        shop = pricing.load_shop(conn)
        if len(query) < 2:
            return line, pricing.rank_products(line.name_latin, shop, pricing.MATCH_CANDIDATES_MAX)
        found = pricing.rank_products(query, shop, pricing.SEARCH_RESULTS_MAX)
        return line, [(product, score) for product, score in found if score >= pricing.SEARCH_SCORE_MIN]

    line, candidates = await db_call(load)
    if line is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "admin/_match_results.html", {"line": line, "candidates": candidates})


@router.post("/admin/invoices/items/{item_id}/match", response_class=HTMLResponse)
async def save_match(request: Request, item_id: int, product_id: str = Form("")) -> Response:
    """Remember the shop product for an invoice line's product, or that the shop does not sell it."""
    require_admin(request)
    chosen = int(product_id) if product_id.strip().isdigit() else None

    def work(conn: sqlite3.Connection) -> tuple[PriceLine | None, int, str]:
        try:
            pricing.set_match(conn, item_id, chosen)
            problem = ""
        except ReviewError as error:
            problem = str(error)
        line, count = _card_with_count(conn, item_id)
        return line, count, problem

    line, count, problem = await db_call(work)
    if line is None:
        return HTMLResponse(_toast("This invoice line no longer exists.", "error"))
    close = '<div id="sheet" hx-swap-oob="innerHTML"></div>'
    if problem:
        return _card(line, close + _toast(problem, "error") + _fix_count(count))
    message = f"Matched to {line.shop.label}" if line.shop else "Marked as not sold in the shop"
    return _card(line, close + _toast(message) + _fix_count(count))
