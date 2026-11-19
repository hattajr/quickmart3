"""Group parsed pages into one checked invoice per supplier and date, and store them."""

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.services.supplier_invoices.parse import KnownProducts, Page

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@dataclass
class Invoice:
    supplier: str
    invoice_date: str
    invoice_no: str
    document_type: str
    page_count: int
    total_qty: int
    total_amount: int
    pages: list[Page]


def _files(pages: list[Page]) -> str:
    return ", ".join(p.source_file for p in pages)


def _pick_totals(pages: list[Page], qty_sum: int, amount_sum: int) -> tuple[int, int] | str:
    """Return the invoice's printed totals, or a problem when they can't be settled."""
    totals = {(p.total_qty, p.total_amount) for p in pages if p.total_qty is not None and p.total_amount is not None}
    if not totals:
        return "no page shows the invoice totals"
    if len(totals) == 1:
        total_qty, total_amount = totals.pop()
        if total_qty is None or total_amount is None:
            raise RuntimeError("totals filtered for None still hold None")
        return total_qty, total_amount
    # A page can show a balance or subtotal that the model mistakes for the total;
    # the printed total that the items add up to is the real one.
    if (qty_sum, amount_sum) in totals:
        logger.warning(
            "%s %s: pages disagree on totals %s, using the one the items add up to",
            pages[0].supplier, pages[0].invoice_date, sorted(totals, key=str),
        )  # fmt: skip
        return qty_sum, amount_sum
    return f"pages disagree on totals {sorted(totals, key=str)} and none matches the items"


def merge_version(pages: list[Page]) -> tuple[Invoice | None, list[str]]:
    """Merge the pages of one invoice version and return it with any problems found."""
    if not pages:
        raise ValueError("merge_version needs at least one page")
    pages = sorted(pages, key=lambda p: p.page_no)
    first = pages[0]
    missing = sorted(set(range(1, first.page_count + 1)) - {p.page_no for p in pages})
    if missing:
        logger.warning("%s %s: no photo for page(s) %s", first.supplier, first.invoice_date, missing)

    items = [item for page in pages for item in page.items]
    qty_sum, amount_sum = sum(i.qty for i in items), sum(i.amount for i in items)
    totals = _pick_totals(pages, qty_sum, amount_sum)
    if isinstance(totals, str):
        return None, [totals]
    total_qty, total_amount = totals

    problems: list[str] = []
    for page in pages:
        for line_no, item in enumerate(page.items, start=1):
            if item.qty * item.unit_price != item.amount:
                problems.append(
                    f"{page.source_file} line {line_no} {item.product_name}: "
                    f"{item.qty} x {item.unit_price} != {item.amount}"
                )
    if qty_sum != total_qty:
        problems.append(f"qty sum {qty_sum} != total {total_qty}")
    if amount_sum != total_amount:
        problems.append(f"amount sum {amount_sum} != total {total_amount}")
    invoice = Invoice(
        supplier=first.supplier,
        invoice_date=first.invoice_date,
        invoice_no=first.invoice_no,
        document_type=first.document_type,
        page_count=first.page_count,
        total_qty=total_qty,
        total_amount=total_amount,
        pages=pages,
    )
    return invoice, problems


def _versions(group: list[Page]) -> dict[tuple[str, int], list[Page]]:
    """Split one date's pages into printings (same number and page count), one photo per page."""
    versions: dict[tuple[str, int], dict[int, Page]] = defaultdict(dict)
    for page in sorted(group, key=lambda p: p.source_file):
        version = versions[(page.invoice_no, page.page_count)]
        if page.page_no in version:
            logger.info(
                "%s: duplicate photo of %s %s page %d, ignored",
                page.source_file, page.supplier, page.invoice_date, page.page_no,
            )  # fmt: skip
            continue
        version[page.page_no] = page
    return {key: list(by_page.values()) for key, by_page in versions.items()}


def build_invoices(pages: list[Page]) -> tuple[list[Invoice], int]:
    """Group pages into one invoice per supplier and date, and count dates that failed."""
    by_key: dict[tuple[str, str], list[Page]] = defaultdict(list)
    for page in pages:
        by_key[(page.supplier, page.invoice_date)].append(page)

    invoices: list[Invoice] = []
    failures = 0
    for (supplier, invoice_date), group in sorted(by_key.items()):
        valid: list[Invoice] = []
        for (invoice_no, page_count), version_pages in _versions(group).items():
            invoice, problems = merge_version(version_pages)
            if invoice is None or problems:
                label = f"{supplier} {invoice_date} invoice {invoice_no} ({page_count} page)"
                for problem in problems:
                    logger.error("%s from %s: %s", label, _files(version_pages), problem)
                continue
            valid.append(invoice)
        if not valid:
            failures += 1
            continue
        # A later copy of the same date's invoice is a revision (e.g. the paper copy delivered
        # with the goods after a morning pre-order), so the newest photo wins.
        valid.sort(key=lambda v: max(p.source_file for p in v.pages))
        chosen = valid[-1]
        for dropped in valid[:-1]:
            logger.warning(
                "%s %s: kept %d items %s won from %s, dropped older version %d items %s won from %s",
                supplier, invoice_date,
                chosen.total_qty, f"{chosen.total_amount:,}", _files(chosen.pages),
                dropped.total_qty, f"{dropped.total_amount:,}", _files(dropped.pages),
            )  # fmt: skip
        invoices.append(chosen)
    return invoices, failures


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the app schema, which creates the supplier tables if they are missing."""
    conn.executescript(SCHEMA_PATH.read_text())


def is_photo_stored(conn: sqlite3.Connection, source_sha256: str) -> bool:
    """Return whether a photo with this hash is already stored."""
    row = conn.execute("SELECT 1 FROM supplier_invoice_pages WHERE source_sha256 = ?", (source_sha256,)).fetchone()
    return row is not None


def is_invoice_stored(conn: sqlite3.Connection, supplier: str, invoice_date: str) -> bool:
    """Return whether this supplier already has an invoice for this date."""
    row = conn.execute(
        "SELECT 1 FROM supplier_invoices WHERE supplier = ? AND invoice_date = ?", (supplier, invoice_date)
    ).fetchone()
    return row is not None


def load_known_products(conn: sqlite3.Connection) -> KnownProducts:
    """Return stored products keyed by (supplier, name_latin) with their latest printed name."""
    rows = conn.execute(
        "SELECT p.supplier, p.name_latin, i.product_name"
        " FROM supplier_products p"
        " JOIN supplier_invoice_items i ON i.product_id = p.id"
        " JOIN supplier_invoices v ON v.id = i.invoice_id"
        " ORDER BY v.invoice_date, i.id"
    ).fetchall()
    return {(supplier, name_latin): product_name for supplier, name_latin, product_name in rows}


def _product_id(conn: sqlite3.Connection, supplier: str, name_latin: str) -> int:
    conn.execute("INSERT OR IGNORE INTO supplier_products (supplier, name_latin) VALUES (?, ?)", (supplier, name_latin))
    row = conn.execute(
        "SELECT id FROM supplier_products WHERE supplier = ? AND name_latin = ?", (supplier, name_latin)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"product {supplier} {name_latin!r} missing right after insert")
    return int(row[0])


def store_invoice(conn: sqlite3.Connection, invoice: Invoice, model: str) -> int:
    """Insert the invoice, its page photos and its items in one transaction and return its id."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO supplier_invoices"
            " (supplier, invoice_date, invoice_no, document_type, total_qty, total_amount, page_count, model)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                invoice.supplier, invoice.invoice_date, invoice.invoice_no, invoice.document_type,
                invoice.total_qty, invoice.total_amount, invoice.page_count, model,
            ),
        )  # fmt: skip
        invoice_id = cursor.lastrowid
        if invoice_id is None:
            raise RuntimeError("insert into supplier_invoices returned no id")
        conn.executemany(
            "INSERT INTO supplier_invoice_pages"
            " (invoice_id, page_no, source_file, source_sha256, rotation_deg,"
            "  image_jpeg, image_width_px, image_height_px, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    invoice_id, p.page_no, p.source_file, p.source_sha256, p.rotation_deg,
                    p.image_jpeg, p.image_width_px, p.image_height_px, p.raw_json,
                )
                for p in invoice.pages
            ],
        )  # fmt: skip
        rows = [
            (
                invoice_id, _product_id(conn, invoice.supplier, item.name_latin), page.page_no, line_no,
                item.product_name, item.items_per_box, item.box, item.qty, item.unit_price, item.amount,
                item.note, int(item.struck_out),
            )
            for page in invoice.pages
            for line_no, item in enumerate(page.items, start=1)
        ]  # fmt: skip
        conn.executemany(
            "INSERT INTO supplier_invoice_items"
            " (invoice_id, product_id, page_no, line_no, product_name, items_per_box,"
            "  box, qty, unit_price, amount, note, struck_out)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return invoice_id
