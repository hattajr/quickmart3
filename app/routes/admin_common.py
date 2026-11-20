"""Shared pieces of the admin pages: templates, login checks and database calls."""

import asyncio
import re
import secrets
import sqlite3
from collections.abc import Callable
from datetime import date

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.config import ADMIN_PASSWORD
from app.db.database import get_db
from app.services.supplier_invoices.pricing import MARKUP_FLAG_PCT, MARKUP_SUGGEST_PCT
from app.services.supplier_invoices.reader_status import PROBLEM_RETRY_SECONDS, PROBLEM_TITLES

templates: Jinja2Templates = Jinja2Templates(directory="app/templates")


def won(value: object) -> str:
    """Format won with thousands separators."""
    return f"{int(str(value)):,}"


def nice_date(value: object) -> str:
    """Format YYYY-MM-DD as '19 Nov 2026'."""
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{parsed.day} {parsed:%b %Y}"


templates.env.filters["won"] = won
templates.env.filters["nice_date"] = nice_date
templates.env.globals["markup_flag_pct"] = MARKUP_FLAG_PCT
templates.env.globals["markup_suggest_pct"] = MARKUP_SUGGEST_PCT
templates.env.globals["reader_problem_titles"] = PROBLEM_TITLES
templates.env.globals["reader_retry_minutes"] = {kind: s // 60 for kind, s in PROBLEM_RETRY_SECONDS.items()}


def verify_password(password: str) -> bool:
    """Compare the admin password in constant time."""
    return secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode())


def is_admin(request: Request) -> bool:
    """Return whether this session has logged in to the admin."""
    return request.session.get("admin_authenticated") is True


def require_admin(request: Request) -> None:
    """Reject a request from a session that has not logged in."""
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def safe_next(path: str) -> str:
    """Return an admin path to go to after login, never another site."""
    if re.fullmatch(r"/admin(/[A-Za-z0-9_\-/]*)?", path or ""):
        return path
    return "/admin/products"


def parse_won(text: str) -> int | None:
    """Read a typed price such as '1,800' or '₩1800'; None when it holds no digits."""
    digits = re.sub(r"\D", "", text or "")
    if not digits or len(digits) > 12:
        return None
    return int(digits)


def run_db[T](work: Callable[[sqlite3.Connection], T]) -> T:
    """Run work on a fresh connection and close it."""
    connections = get_db()
    conn = next(connections)
    try:
        return work(conn)
    finally:
        connections.close()


async def db_call[T](work: Callable[[sqlite3.Connection], T]) -> T:
    """Run database work in a thread so the event loop keeps serving."""
    return await asyncio.to_thread(run_db, work)
