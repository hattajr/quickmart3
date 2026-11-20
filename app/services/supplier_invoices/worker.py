"""Read invoice photos sent from the admin page, one upload at a time, with headless Claude Code.

The app only queues photos. This runs as the `invoice-worker` container (signed in with
CLAUDE_CODE_OAUTH_TOKEN), or on a dev machine where `claude` is logged in.
Usage: uv run python -m app.services.supplier_invoices.worker --db data/quickmart.sqlite3
"""

import argparse
import fcntl
import logging
import signal
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from app.services.supplier_invoices.ledger import (
    apply_schema,
    build_invoices,
    connect,
    invoice_id_for_date,
    invoice_id_for_photo,
    load_known_products,
    store_invoice,
)
from app.services.supplier_invoices.parse import PAGE_MODEL_DEFAULT, ClaudeError, Page, parse_photo
from app.services.supplier_invoices.reader_status import PROBLEM_RETRY_SECONDS, PROBLEM_TITLES, Heartbeat, status_path
from app.services.supplier_invoices.uploads import (
    claim_next,
    fail_upload,
    finish_upload,
    recover_interrupted,
    requeue_upload,
    set_progress,
    upload_photos,
)

logger = logging.getLogger(__name__)

POLL_SECONDS_DEFAULT = 5
POLL_SECONDS_MAX = 300
ERROR_CHARS_MAX = 200


def _short(error: Exception) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= ERROR_CHARS_MAX else text[: ERROR_CHARS_MAX - 1] + "…"


def _read_pages(
    conn: sqlite3.Connection, upload_id: int, model: str, rotate: str, cache_dir: Path
) -> tuple[list[Page], set[int], list[str]]:
    """Parse the upload's photos; return pages, invoices that already hold a photo, and problems."""
    photos = upload_photos(conn, upload_id)
    if not photos:
        return [], set(), ["This upload has no photos left. Send them again."]
    known = load_known_products(conn)
    pages: list[Page] = []
    already: set[int] = set()
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for photo in photos:
            stored_in = invoice_id_for_photo(conn, photo.sha256)
            if stored_in is not None:
                already.add(stored_in)
                continue
            set_progress(conn, upload_id, f"Reading photo {photo.position} of {len(photos)}")
            path = Path(tmp) / photo.file_name
            path.write_bytes(photo.image)
            try:
                page = parse_photo(path, photo.sha256, rotate, model, cache_dir, known)
            except ClaudeError as error:
                if error.kind in PROBLEM_RETRY_SECONDS:
                    raise
                logger.exception("upload %d photo %d: claude failed", upload_id, photo.position)
                problems.append(f"Photo {photo.position}: {_short(error)}")
                continue
            except Exception as error:
                logger.exception("upload %d photo %d: parse failed", upload_id, photo.position)
                problems.append(f"Photo {photo.position}: {_short(error)}")
                continue
            pages.append(page)
            # Later photos reuse the names read from earlier ones, as in the command line ingest.
            for item in page.items:
                known[(page.supplier, item.name_latin)] = item.product_name
    return pages, already, problems


def process_upload(conn: sqlite3.Connection, upload_id: int, model: str, rotate: str, cache_dir: Path) -> None:
    """Read one claimed upload, store its invoice, and mark the upload done or failed."""
    pages, already, problems = _read_pages(conn, upload_id, model, rotate, cache_dir)
    invoices, failures = build_invoices(pages)
    problems.extend(failures)
    stored: list[int] = []
    for invoice in invoices:
        existing = invoice_id_for_date(conn, invoice.supplier, invoice.invoice_date)
        if existing is not None:
            already.add(existing)
            continue
        stored.append(store_invoice(conn, invoice, model))
        logger.info(
            "upload %d: stored invoice %d, %s %s, %d items",
            upload_id, stored[-1], invoice.supplier, invoice.invoice_date, sum(len(p.items) for p in invoice.pages),
        )  # fmt: skip

    if not stored and not already:
        fail_upload(conn, upload_id, "; ".join(problems) or "No invoice was found in these photos.")
        logger.warning("upload %d failed: %s", upload_id, problems)
        return
    notes: list[str] = []
    if not stored:
        notes.append("This invoice was added before, so nothing new was stored.")
    if problems:
        notes.append("Some photos could not be used: " + "; ".join(problems))
    finish_upload(conn, upload_id, stored[-1] if stored else max(already), " ".join(notes))


class _Stop:
    requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        logger.info("signal %d received, stopping after the current upload", signum)
        self.requested = True


def handle_upload(
    conn: sqlite3.Connection, upload_id: int, model: str, rotate: str, cache_dir: Path, heartbeat: Heartbeat
) -> int:
    """Read one claimed upload; return the seconds to wait before the next one, 0 to go on."""
    try:
        process_upload(conn, upload_id, model, rotate, cache_dir)
    except ClaudeError as error:
        if error.kind in PROBLEM_RETRY_SECONDS:
            requeue_upload(conn, upload_id, f"{PROBLEM_TITLES[error.kind]}: {error}")
            heartbeat.set_problem(error.kind, str(error))
            logger.warning("upload %d back in line, claude %s problem: %s", upload_id, error.kind, error)
            return PROBLEM_RETRY_SECONDS[error.kind]
        logger.exception("upload %d: claude failed", upload_id)
        fail_upload(conn, upload_id, f"Claude failed: {_short(error)}")
        return 0
    except Exception as error:
        logger.exception("upload %d: worker error", upload_id)
        fail_upload(conn, upload_id, f"The reader hit an error: {_short(error)}")
        return 0
    heartbeat.clear_problem()
    return 0


def _wait(stop: _Stop, seconds: int) -> None:
    for _ in range(seconds):
        if stop.requested:
            return
        time.sleep(1)


@dataclass(frozen=True)
class Settings:
    model: str
    rotate: str
    cache_dir: Path
    poll_seconds: int
    once: bool


def _serve(conn: sqlite3.Connection, heartbeat: Heartbeat, stop: _Stop, settings: Settings) -> None:
    apply_schema(conn)
    # Only one worker runs (the lock in run), so anything still marked as being read was cut off.
    requeued = recover_interrupted(conn)
    if requeued:
        logger.info("requeued %d upload(s) a previous run left half read", requeued)
    while not stop.requested:
        upload_id = claim_next(conn)
        if upload_id is None:
            if settings.once:
                return
            _wait(stop, settings.poll_seconds)
            continue
        logger.info("reading upload %d", upload_id)
        pause_seconds = handle_upload(conn, upload_id, settings.model, settings.rotate, settings.cache_dir, heartbeat)
        if pause_seconds:
            if settings.once:
                return
            _wait(stop, pause_seconds)


def run(db_path: Path, cache_dir: Path, model: str, rotate: str, poll_seconds: int, once: bool) -> None:
    """Process uploads until stopped, or until the queue is empty when once is set."""
    stop = _Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cache_dir.mkdir(exist_ok=True)
    lock_path = db_path.parent / ".supplier-invoice-worker.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"another worker holds {lock_path}; only one may run") from None
        conn = connect(db_path)
        heartbeat = Heartbeat(status_path(db_path))
        heartbeat.start()
        logger.info("waiting for invoice uploads in %s", db_path)
        try:
            _serve(conn, heartbeat, stop, Settings(model, rotate, cache_dir, poll_seconds, once))
        finally:
            try:
                heartbeat.stop()
            finally:
                conn.close()


def main() -> int:
    """Read the command line and run the worker."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="SQLite database the app uses")
    parser.add_argument("--model", default=PAGE_MODEL_DEFAULT, help="Claude model that reads the page")
    parser.add_argument("--rotate", default="auto", choices=("auto", "0", "90", "180", "270"))
    parser.add_argument("--cache-dir", type=Path, help="parse cache (default: supplier_invoice_cache beside the db)")
    parser.add_argument("--poll-seconds", type=int, default=POLL_SECONDS_DEFAULT, help="wait between empty checks")
    parser.add_argument("--once", action="store_true", help="exit when no upload is waiting")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not 1 <= args.poll_seconds <= POLL_SECONDS_MAX:
        parser.error(f"--poll-seconds must be 1 to {POLL_SECONDS_MAX}")
    if not args.db.is_file():
        parser.error(f"no database at {args.db}")
    cache_dir: Path = args.cache_dir or args.db.parent / "supplier_invoice_cache"
    run(args.db, cache_dir, args.model, args.rotate, args.poll_seconds, args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
