"""Parse supplier invoice photos with headless Claude Code and append new invoices to the database.

Usage: uv run python -m app.services.supplier_invoices --db data/quickmart.sqlite3 photo.jpg [...]
"""

import argparse
import hashlib
import logging
import sqlite3
import sys
from pathlib import Path

from app.services.supplier_invoices.ledger import (
    apply_schema,
    build_invoices,
    connect,
    is_invoice_stored,
    is_photo_stored,
    load_known_products,
    store_invoice,
)
from app.services.supplier_invoices.parse import PAGE_MODEL_DEFAULT, Page, parse_photo

logger = logging.getLogger(__name__)

PHOTOS_PER_RUN_MAX = 100


def photos_to_parse(conn: sqlite3.Connection, images: list[Path]) -> list[tuple[Path, str]]:
    """Return (photo, sha256) for each photo not stored yet, oldest file name first."""
    todo: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for image_path in sorted(images, key=lambda p: p.name):
        source_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if is_photo_stored(conn, source_sha256):
            logger.info("%s: already stored, skipped", image_path.name)
        elif source_sha256 in seen:
            logger.info("%s: same photo given twice, skipped", image_path.name)
        else:
            seen.add(source_sha256)
            todo.append((image_path, source_sha256))
    return todo


def run(args: argparse.Namespace) -> int:
    """Parse, check and store; return the number of photos or dates that failed."""
    cache_dir: Path = args.cache_dir or args.db.parent / "supplier_invoice_cache"
    cache_dir.mkdir(exist_ok=True)
    conn = connect(args.db)
    try:
        apply_schema(conn)
        todo = photos_to_parse(conn, args.images)
        # One photo at a time, oldest first, so each sees the product names read before it.
        known = load_known_products(conn)
        pages: list[Page] = []
        failures = 0
        for path, source_sha256 in todo:
            try:
                page = parse_photo(path, source_sha256, args.rotate, args.model, cache_dir, known)
            except Exception:
                logger.exception("%s: parse failed", path.name)
                failures += 1
                continue
            pages.append(page)
            for item in page.items:
                known[(page.supplier, item.name_latin)] = item.product_name

        invoices, date_failures = build_invoices(pages)
        failures += len(date_failures)
        for invoice in invoices:
            if is_invoice_stored(conn, invoice.supplier, invoice.invoice_date):
                logger.info(
                    "%s %s: an invoice for this date is already stored, skipped",
                    invoice.supplier, invoice.invoice_date,
                )  # fmt: skip
                continue
            invoice_id = store_invoice(conn, invoice, args.model)
            logger.info(
                "stored invoice %d: %s %s, %d page(s), %d items, %s won",
                invoice_id, invoice.supplier, invoice.invoice_date, len(invoice.pages),
                sum(len(p.items) for p in invoice.pages), f"{invoice.total_amount:,}",
            )  # fmt: skip
        return failures
    finally:
        conn.close()


def main() -> int:
    """Read the command line and run the ingest."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="+", type=Path, help="invoice photos; pages of one invoice may be separate")
    parser.add_argument("--db", type=Path, required=True, help="SQLite database to write, e.g. data/quickmart.sqlite3")
    parser.add_argument("--model", default=PAGE_MODEL_DEFAULT, help="Claude model that reads the page")
    parser.add_argument(
        "--rotate",
        default="auto",
        choices=("auto", "0", "90", "180", "270"),
        help="clockwise degrees to make photos upright, or auto to ask a fast model (default)",
    )
    parser.add_argument(
        "--cache-dir", type=Path, help="where parsed answers are cached (default: supplier_invoice_cache beside the db)"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(args.images) > PHOTOS_PER_RUN_MAX:
        parser.error(f"at most {PHOTOS_PER_RUN_MAX} photos per run")
    missing = [str(p) for p in args.images if not p.is_file()]
    if missing:
        parser.error(f"no such photo: {', '.join(missing)}")
    return 1 if run(args) else 0


if __name__ == "__main__":
    sys.exit(main())
