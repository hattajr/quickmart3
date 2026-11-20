"""Tests for the invoice photo queue and the worker that reads it."""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.supplier_invoices import parse, reader_status, uploads, worker  # noqa: E402
from app.services.supplier_invoices.ledger import apply_schema  # noqa: E402
from app.services.supplier_invoices.parse import ClaudeError, Item, KnownProducts, Page  # noqa: E402


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
    return Page(
        source.name, sha256, 0, b"jpeg", 1, 1, "{}", "하나피아", "거래명세표", "1", invoice_date, 1, 1, 1, 37000, items
    )


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


# What Claude Code 2.1.329 prints with no login (trimmed to the fields that matter).
NOT_LOGGED_IN = json.dumps(
    {"type": "result", "is_error": True, "api_error_status": None, "result": "Not logged in · Please run /login"}
)


def test_claude_failure_sorts_errors_by_what_to_do() -> None:
    login = parse.claude_failure(NOT_LOGGED_IN, "", 1)
    assert (login.kind, str(login)) == ("login", "Not logged in · Please run /login")
    limit = parse.claude_failure(json.dumps({"result": "Request failed", "api_error_status": 429}), "", 1)
    assert limit.kind == "limit"
    assert parse.claude_failure(json.dumps({"result": "Overloaded", "api_error_status": 529}), "", 1).kind == "busy"
    assert parse.claude_failure(json.dumps({"result": "You've hit your limit · resets 3pm"}), "", 1).kind == "limit"


def test_claude_failure_handles_plain_text_and_silence() -> None:
    plain = parse.claude_failure("not json", "segfault in   bun\n", 139)
    assert (plain.kind, str(plain)) == ("other", "segfault in bun")
    assert str(parse.claude_failure("", "", 2)) == "claude exited 2 without a message"
    assert len(str(parse.claude_failure("", "x" * 5000, 1))) == parse.CLAUDE_MESSAGE_CHARS_MAX
    with pytest.raises(ValueError, match="unknown claude error kind"):
        ClaudeError("weird", "x")


def test_run_claude_explains_a_missing_program_and_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("claude")

    def slow(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["claude", "-p", "a very long prompt"], parse.CLAUDE_TIMEOUT_SECONDS)

    monkeypatch.setattr(parse.subprocess, "run", missing)
    with pytest.raises(ClaudeError) as gone:
        parse.run_claude("read it", {}, "opus", tmp_path)
    assert gone.value.kind == "missing"
    monkeypatch.setattr(parse.subprocess, "run", slow)
    with pytest.raises(ClaudeError) as late:
        parse.run_claude("read it", {}, "opus", tmp_path)
    assert late.value.kind == "timeout" and "long prompt" not in str(late.value)


def test_requeue_puts_an_upload_back_without_using_an_attempt(conn: sqlite3.Connection) -> None:
    upload_id = uploads.create_upload(conn, [b"photo"])
    uploads.claim_next(conn)
    uploads.requeue_upload(conn, upload_id, "The reader can't sign in to Claude: Not logged in")
    back = uploads.get_upload(conn, upload_id)
    assert back is not None and (back.status, back.attempts) == ("waiting", 0)
    assert back.message.startswith("The reader can't sign in")
    with pytest.raises(RuntimeError, match="not being read"):
        uploads.requeue_upload(conn, upload_id, "again")


def test_reader_status_reads_fresh_old_missing_and_broken_files(tmp_path: Path) -> None:
    path = reader_status.status_path(tmp_path / "quickmart.sqlite3")
    assert reader_status.read_status(path, time.time()) == reader_status.NOT_RUNNING
    reader_status.write_status(path, "login", "Not logged in", 1000.0)
    assert reader_status.read_status(path, 1010.0) == reader_status.ReaderStatus(True, "login", "Not logged in")
    assert not reader_status.read_status(path, 1000.0 + reader_status.STALE_SECONDS + 1).running
    path.write_text("{half a file")
    assert reader_status.read_status(path, 1010.0) == reader_status.NOT_RUNNING
    path.write_text(json.dumps({"beat_at": "soon", "problem_kind": "", "problem": ""}))
    assert reader_status.read_status(path, 1010.0) == reader_status.NOT_RUNNING
    with pytest.raises(ValueError, match="both a kind and a message"):
        reader_status.write_status(path, "login", "", 1000.0)


def test_heartbeat_shows_a_problem_and_removes_its_file_on_stop(tmp_path: Path) -> None:
    path = reader_status.status_path(tmp_path / "quickmart.sqlite3")
    heartbeat = reader_status.Heartbeat(path)
    heartbeat.start()
    assert reader_status.read_status(path, time.time()) == reader_status.ReaderStatus(True, "", "")
    heartbeat.set_problem("limit", "You've hit your limit")
    assert reader_status.read_status(path, time.time()).problem_kind == "limit"
    heartbeat.clear_problem()
    assert reader_status.read_status(path, time.time()).problem_kind == ""
    heartbeat.stop()
    assert not path.exists()
    with pytest.raises(ValueError, match="unknown problem kind"):
        heartbeat.set_problem("timeout", "x")


def test_worker_waits_on_a_login_problem_then_reads_once_it_is_fixed(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_logged_in(*args: object, **kwargs: object) -> Page:
        raise parse.claude_failure(NOT_LOGGED_IN, "", 1)

    path = reader_status.status_path(tmp_path / "quickmart.sqlite3")
    heartbeat = reader_status.Heartbeat(path)
    upload_id = uploads.create_upload(conn, [b"2026-11-19"])
    monkeypatch.setattr(worker, "parse_photo", not_logged_in)
    assert uploads.claim_next(conn) == upload_id
    pause = worker.handle_upload(conn, upload_id, "opus", "auto", tmp_path, heartbeat)
    assert pause == reader_status.PROBLEM_RETRY_SECONDS["login"]
    waiting = uploads.get_upload(conn, upload_id)
    assert waiting is not None and waiting.status == "waiting"
    assert "can't sign in to Claude: Not logged in" in waiting.message
    assert reader_status.read_status(path, time.time()).problem_kind == "login"

    monkeypatch.setattr(worker, "parse_photo", fake_parse)
    assert uploads.claim_next(conn) == upload_id
    assert worker.handle_upload(conn, upload_id, "opus", "auto", tmp_path, heartbeat) == 0
    done = uploads.get_upload(conn, upload_id)
    assert done is not None and done.status == "done"
    assert reader_status.read_status(path, time.time()).problem_kind == ""


def test_worker_fails_an_upload_when_claude_times_out_on_a_photo(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_slow(*args: object, **kwargs: object) -> Page:
        raise ClaudeError("timeout", "Claude took longer than 10 minutes on this photo.")

    monkeypatch.setattr(worker, "parse_photo", too_slow)
    upload_id = uploads.create_upload(conn, [b"2026-11-19"])
    uploads.claim_next(conn)
    heartbeat = reader_status.Heartbeat(reader_status.status_path(tmp_path / "quickmart.sqlite3"))
    assert worker.handle_upload(conn, upload_id, "opus", "auto", tmp_path, heartbeat) == 0
    failed = uploads.get_upload(conn, upload_id)
    assert failed is not None and failed.status == "failed"
    assert failed.message == "Photo 1: Claude took longer than 10 minutes on this photo."
