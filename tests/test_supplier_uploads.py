"""Tests for the invoice photo queue and the worker that reads it."""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.supplier_invoices import uploads, worker  # noqa: E402
from app.services.supplier_invoices.ledger import apply_schema  # noqa: E402
from app.services.supplier_invoices.parse import Item, KnownProducts, Page  # noqa: E402


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON;")
    apply_schema(connection)
    return connection


def status(conn: sqlite3.Connection, upload_id: int) -> str:
    upload = uploads.get_upload(conn, upload_id)
    assert upload is not None
    return upload.status


def test_create_upload_checks_photo_count_and_size(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        uploads.create_upload(conn, [])
    with pytest.raises(ValueError):
        uploads.create_upload(conn, [b"x"] * (uploads.PHOTOS_PER_UPLOAD_MAX + 1))
    with pytest.raises(ValueError):
        uploads.create_upload(conn, [b""])
    upload_id = uploads.create_upload(conn, [b"one", b"two"])
    photos = uploads.upload_photos(conn, upload_id)
    assert [p.position for p in photos] == [1, 2]
    assert photos[0].file_name == f"upload-{upload_id:06d}-1.jpg"


def test_claim_takes_the_oldest_waiting_upload(conn: sqlite3.Connection) -> None:
    first = uploads.create_upload(conn, [b"a"])
    second = uploads.create_upload(conn, [b"b"])
    assert uploads.claim_next(conn) == first
    assert status(conn, first) == "reading"
    assert uploads.claim_next(conn) == second
    assert uploads.claim_next(conn) is None
    assert [u.id for u in uploads.open_uploads(conn)] == [second, first]


def test_finish_links_invoice_and_drops_photos(conn: sqlite3.Connection) -> None:
    upload_id = uploads.create_upload(conn, [b"a"])
    conn.execute(
        "INSERT INTO supplier_invoices (id, supplier, invoice_date, invoice_no, document_type, total_qty,"
        " total_amount, page_count, model) VALUES (7, 's', '2026-11-19', '1', 't', 1, 1, 1, 'opus')"
    )
    conn.commit()
    with pytest.raises(RuntimeError):
        uploads.finish_upload(conn, upload_id, 7, "")
    uploads.claim_next(conn)
    uploads.finish_upload(conn, upload_id, 7, "")
    upload = uploads.get_upload(conn, upload_id)
    assert upload is not None and (upload.status, upload.invoice_id, upload.photo_count) == ("done", 7, 0)
    assert uploads.open_uploads(conn) == []


def test_failed_upload_can_be_retried_or_deleted(conn: sqlite3.Connection) -> None:
    upload_id = uploads.create_upload(conn, [b"a"])
    uploads.claim_next(conn)
    assert not uploads.delete_upload(conn, upload_id)
    uploads.fail_upload(conn, upload_id, "totals do not match")
    assert status(conn, upload_id) == "failed"
    assert uploads.retry_upload(conn, upload_id)
    assert status(conn, upload_id) == "waiting"
    assert not uploads.retry_upload(conn, upload_id)
    assert uploads.delete_upload(conn, upload_id)
    assert conn.execute("SELECT count(*) FROM supplier_invoice_upload_photos").fetchone() == (0,)


def test_recover_requeues_interrupted_uploads_until_attempts_run_out(conn: sqlite3.Connection) -> None:
    fresh = uploads.create_upload(conn, [b"a"])
    worn = uploads.create_upload(conn, [b"b"])
    uploads.claim_next(conn)
    uploads.claim_next(conn)
    conn.execute("UPDATE supplier_invoice_uploads SET attempts = ? WHERE id = ?", (uploads.ATTEMPTS_MAX, worn))
    assert uploads.recover_interrupted(conn) == 1
    assert (status(conn, fresh), status(conn, worn)) == ("waiting", "failed")


def fake_parse(source: Path, sha256: str, rotate: str, model: str, cache_dir: Path, known: KnownProducts) -> Page:
    """Stand in for claude -p: every photo is a one-page invoice whose date is in its bytes."""
    invoice_date = source.read_bytes().decode()
    if invoice_date == "unreadable":
        raise RuntimeError("claude exited 1: could not read the page")
    items = [Item("코코", "Hydro Coco 330ml", "24 EA", "1", 1, 37000, 37000, "", False)]
    return Page(source.name, sha256, 0, b"jpeg", 1, 1, "{}", "하나피아", "거래명세표", "1", invoice_date, 1, 1, 1, 37000, items)


def test_worker_stores_invoice_then_recognises_the_same_photo(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "parse_photo", fake_parse)
    first = uploads.create_upload(conn, [b"2026-11-19"])
    assert uploads.claim_next(conn) == first
    worker.process_upload(conn, first, "opus", "auto", tmp_path)
    done = uploads.get_upload(conn, first)
    assert done is not None and done.status == "done" and done.invoice_id is not None
    assert conn.execute("SELECT count(*) FROM supplier_invoice_items").fetchone() == (1,)

    again = uploads.create_upload(conn, [b"2026-11-19"])
    uploads.claim_next(conn)
    worker.process_upload(conn, again, "opus", "auto", tmp_path)
    repeat = uploads.get_upload(conn, again)
    assert repeat is not None and repeat.status == "done"
    assert repeat.invoice_id == done.invoice_id
    assert "added before" in repeat.message


def test_worker_fails_an_upload_it_cannot_read(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "parse_photo", fake_parse)
    upload_id = uploads.create_upload(conn, [b"unreadable"])
    uploads.claim_next(conn)
    worker.process_upload(conn, upload_id, "opus", "auto", tmp_path)
    failed = uploads.get_upload(conn, upload_id)
    assert failed is not None and failed.status == "failed"
    assert "Photo 1" in failed.message and failed.photo_count == 1
