"""Queue of invoice photos sent from the admin page, read later by the invoice worker."""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

PHOTOS_PER_UPLOAD_MAX = 6
PHOTO_BYTES_MAX = 12 * 1024 * 1024
MESSAGE_CHARS_MAX = 600
ATTEMPTS_MAX = 3
STATUSES = ("waiting", "reading", "done", "failed")


@dataclass(frozen=True)
class Upload:
    id: int
    status: str
    attempts: int
    invoice_id: int | None
    message: str
    created_at: str
    photo_count: int

    def waited_minutes(self, now: datetime | None = None) -> int:
        """Return whole minutes since the photos were sent."""
        created = datetime.strptime(self.created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return max(0, int(((now or datetime.now(UTC)) - created).total_seconds() // 60))


@dataclass(frozen=True)
class UploadPhoto:
    position: int
    file_name: str
    image: bytes
    sha256: str


def _clip(message: str) -> str:
    message = " ".join(message.split())
    return message if len(message) <= MESSAGE_CHARS_MAX else message[: MESSAGE_CHARS_MAX - 1] + "…"


def create_upload(conn: sqlite3.Connection, images: list[bytes]) -> int:
    """Queue one invoice's photos, in page order, and return the upload id."""
    if not 1 <= len(images) <= PHOTOS_PER_UPLOAD_MAX:
        raise ValueError(f"send 1 to {PHOTOS_PER_UPLOAD_MAX} photos, got {len(images)}")
    for image in images:
        if not 0 < len(image) <= PHOTO_BYTES_MAX:
            raise ValueError(f"each photo must be 1 byte to {PHOTO_BYTES_MAX // (1024 * 1024)} MB")
    with conn:
        cursor = conn.execute("INSERT INTO supplier_invoice_uploads (status) VALUES ('waiting')")
        upload_id = cursor.lastrowid
        if upload_id is None:
            raise RuntimeError("insert into supplier_invoice_uploads returned no id")
        # Zero-padded names sort in upload order, which build_invoices uses to pick the newest copy.
        conn.executemany(
            "INSERT INTO supplier_invoice_upload_photos (upload_id, position, file_name, image, sha256)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (
                    upload_id,
                    position,
                    f"upload-{upload_id:06d}-{position}.jpg",
                    image,
                    hashlib.sha256(image).hexdigest(),
                )
                for position, image in enumerate(images, start=1)
            ],
        )
    return upload_id


_UPLOAD_COLUMNS = (
    "u.id, u.status, u.attempts, u.invoice_id, u.message, u.created_at,"
    " (SELECT count(*) FROM supplier_invoice_upload_photos p WHERE p.upload_id = u.id)"
)


def _upload(row: tuple[object, ...]) -> Upload:
    upload_id, status, attempts, invoice_id, message, created_at, photo_count = row
    return Upload(
        id=int(str(upload_id)),
        status=str(status),
        attempts=int(str(attempts)),
        invoice_id=None if invoice_id is None else int(str(invoice_id)),
        message=str(message),
        created_at=str(created_at),
        photo_count=int(str(photo_count)),
    )


def get_upload(conn: sqlite3.Connection, upload_id: int) -> Upload | None:
    """Return one upload, or None when it does not exist."""
    row = conn.execute(f"SELECT {_UPLOAD_COLUMNS} FROM supplier_invoice_uploads u WHERE u.id = ?", (upload_id,))
    found = row.fetchone()
    return None if found is None else _upload(tuple(found))


def open_uploads(conn: sqlite3.Connection, limit: int = 20) -> list[Upload]:
    """Return uploads still waiting, being read, or failed, newest first."""
    if not 1 <= limit <= 100:
        raise ValueError(f"limit must be 1 to 100, got {limit}")
    rows = conn.execute(
        f"SELECT {_UPLOAD_COLUMNS} FROM supplier_invoice_uploads u"
        " WHERE u.status IN ('waiting', 'reading', 'failed') ORDER BY u.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_upload(tuple(row)) for row in rows]


def upload_photos(conn: sqlite3.Connection, upload_id: int) -> list[UploadPhoto]:
    """Return an upload's photos in page order."""
    rows = conn.execute(
        "SELECT position, file_name, image, sha256 FROM supplier_invoice_upload_photos"
        " WHERE upload_id = ? ORDER BY position",
        (upload_id,),
    ).fetchall()
    return [UploadPhoto(int(r[0]), str(r[1]), bytes(r[2]), str(r[3])) for r in rows]


def claim_next(conn: sqlite3.Connection) -> int | None:
    """Mark the oldest waiting upload as being read and return its id, or None when none waits."""
    with conn:
        row = conn.execute(
            "UPDATE supplier_invoice_uploads"
            " SET status = 'reading', attempts = attempts + 1, started_at = CURRENT_TIMESTAMP, message = ''"
            " WHERE id = (SELECT id FROM supplier_invoice_uploads WHERE status = 'waiting' ORDER BY id LIMIT 1)"
            " RETURNING id"
        ).fetchone()
    return None if row is None else int(row[0])


def set_progress(conn: sqlite3.Connection, upload_id: int, message: str) -> None:
    """Show what the worker is doing on an upload it is reading."""
    with conn:
        conn.execute(
            "UPDATE supplier_invoice_uploads SET message = ? WHERE id = ? AND status = 'reading'",
            (_clip(message), upload_id),
        )


def finish_upload(conn: sqlite3.Connection, upload_id: int, invoice_id: int, message: str) -> None:
    """Mark an upload read, link its invoice, and drop its photos (the invoice keeps a copy)."""
    with conn:
        cursor = conn.execute(
            "UPDATE supplier_invoice_uploads SET status = 'done', invoice_id = ?, message = ?,"
            " finished_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'reading'",
            (invoice_id, _clip(message), upload_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"upload {upload_id} is not being read, so it cannot be finished")
        conn.execute("DELETE FROM supplier_invoice_upload_photos WHERE upload_id = ?", (upload_id,))


def fail_upload(conn: sqlite3.Connection, upload_id: int, message: str) -> None:
    """Mark an upload failed with the reason, keeping its photos for a retry."""
    with conn:
        conn.execute(
            "UPDATE supplier_invoice_uploads SET status = 'failed', message = ?, finished_at = CURRENT_TIMESTAMP"
            " WHERE id = ?",
            (_clip(message) or "The invoice could not be read", upload_id),
        )


def requeue_upload(conn: sqlite3.Connection, upload_id: int, reason: str) -> None:
    """Put an upload back in line with the reason it waits, without counting it as an attempt."""
    with conn:
        cursor = conn.execute(
            "UPDATE supplier_invoice_uploads SET status = 'waiting', attempts = max(attempts - 1, 0),"
            " message = ?, started_at = NULL WHERE id = ? AND status = 'reading'",
            (_clip(reason), upload_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"upload {upload_id} is not being read, so it cannot be requeued")


def retry_upload(conn: sqlite3.Connection, upload_id: int) -> bool:
    """Put a failed upload back in the queue; return False when it is not failed or has no photos."""
    with conn:
        cursor = conn.execute(
            "UPDATE supplier_invoice_uploads SET status = 'waiting', attempts = 0, message = '', finished_at = NULL"
            " WHERE id = ? AND status = 'failed'"
            " AND EXISTS (SELECT 1 FROM supplier_invoice_upload_photos p WHERE p.upload_id = ?)",
            (upload_id, upload_id),
        )
    return cursor.rowcount == 1


def delete_upload(conn: sqlite3.Connection, upload_id: int) -> bool:
    """Delete an upload that is not being read; return False when there is nothing to delete."""
    with conn:
        cursor = conn.execute("DELETE FROM supplier_invoice_uploads WHERE id = ? AND status != 'reading'", (upload_id,))
    return cursor.rowcount == 1


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """Requeue uploads a stopped worker left half read, failing those already tried too often."""
    with conn:
        conn.execute(
            "UPDATE supplier_invoice_uploads SET status = 'failed', finished_at = CURRENT_TIMESTAMP,"
            " message = ? WHERE status = 'reading' AND attempts >= ?",
            (f"Reading stopped {ATTEMPTS_MAX} times. Delete it and send the photos again.", ATTEMPTS_MAX),
        )
        cursor = conn.execute(
            "UPDATE supplier_invoice_uploads SET status = 'waiting', message = '' WHERE status = 'reading'"
        )
    return cursor.rowcount
