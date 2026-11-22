"""Admin Popular and Messages tabs: what customers add to the cart, and what they write to the shop."""

import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.config import SESSION_COOKIE_SECURE
from app.routes.admin_common import db_call, is_admin, require_admin, templates
from app.services import customer_activity as activity
from app.services.customer_activity import Period

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter()

POPULAR_FIRST_ROWS = 15
PERIOD_DAYS_DEFAULT = 7
# Lives longer than the 8-hour admin login, so the new-message count survives logging in again.
SEEN_COOKIE = "admin_messages_seen_id"
SEEN_COOKIE_MAX_AGE_S = 365 * 24 * 60 * 60
MESSAGE_ID_MAX = 2**63 - 1


def _shop_year() -> int:
    return activity.to_shop_time(datetime.now(timezone.utc)).year


templates.env.filters["shop_day"] = lambda day: activity.day_label(day, _shop_year())
templates.env.filters["range_label"] = lambda period: activity.range_label(period.first_day, period.last_day)


def _period(days: int) -> Period | None:
    if days not in activity.PERIOD_DAYS_ALLOWED:
        return None
    return activity.period_ending(days, datetime.now(timezone.utc))


def _bad_days() -> PlainTextResponse:
    allowed = ", ".join(str(d) for d in activity.PERIOD_DAYS_ALLOWED)
    return PlainTextResponse(f"Pick one of {allowed} days.", status_code=400)


def _toast(message: str, kind: str = "ok") -> str:
    return templates.get_template("admin/_toast.html").render(message=message, type=kind)


def seen_message_id(cookie: str | None) -> int:
    """Read the newest message id this phone has seen; 0 when it has seen none."""
    text = (cookie or "").strip()
    if not text.isdigit() or len(text) > 19:
        return 0
    value = int(text)
    return value if value <= MESSAGE_ID_MAX else 0


async def _popular(request: Request, period: Period, template: str, show_all: bool) -> Response:
    limit = activity.POPULAR_PRODUCTS_MAX if show_all else POPULAR_FIRST_ROWS
    report = await db_call(lambda conn: activity.popular_products(conn, period, limit))
    return templates.TemplateResponse(
        request,
        template,
        {
            "tab": "popular",
            "report": report,
            "days_allowed": activity.PERIOD_DAYS_ALLOWED,
            "products_max": activity.POPULAR_PRODUCTS_MAX,
        },
    )


@router.get("/admin/popular", response_class=HTMLResponse)
async def popular_page(request: Request, days: int = PERIOD_DAYS_DEFAULT) -> Response:
    """Show the Popular tab: the products customers added to the cart, most taps first."""
    if not is_admin(request):
        return templates.TemplateResponse(request, "admin/login.html", {"next": "/admin/popular", "error": ""})
    period = _period(days)
    if period is None:
        return _bad_days()
    return await _popular(request, period, "admin/popular.html", show_all=False)


@router.get("/admin/popular/list", response_class=HTMLResponse)
async def popular_list(request: Request, days: int = PERIOD_DAYS_DEFAULT, full: int = 0) -> Response:
    """Return the Popular list for other days, or with every product."""
    require_admin(request)
    period = _period(days)
    if period is None:
        return _bad_days()
    return await _popular(request, period, "admin/_popular.html", show_all=full == 1)


@router.get("/admin/popular/{product_id}/sheet", response_class=HTMLResponse)
async def popular_sheet(request: Request, product_id: int, days: int = PERIOD_DAYS_DEFAULT) -> Response:
    """Return one product's taps per day."""
    require_admin(request)
    period = _period(days)
    if period is None:
        return _bad_days()

    def load(conn: sqlite3.Connection) -> tuple[activity.PopularProduct, activity.BarChart] | None:
        product = activity.product_numbers(conn, period, product_id)
        if product is None:
            return None
        return product, activity.bar_chart(activity.daily_taps(conn, period, product_id))

    found = await db_call(load)
    if found is None:
        return HTMLResponse(_toast("That product no longer exists.", "error"))
    product, chart = found
    return templates.TemplateResponse(
        request, "admin/_popular_sheet.html", {"p": product, "chart": chart, "period": period}
    )


@router.get("/admin/messages", response_class=HTMLResponse)
async def messages_page(request: Request) -> Response:
    """Show the Messages tab and remember the newest message as seen on this phone."""
    if not is_admin(request):
        return templates.TemplateResponse(request, "admin/login.html", {"next": "/admin/messages", "error": ""})
    seen_id = seen_message_id(request.cookies.get(SEEN_COOKIE))

    def load(conn: sqlite3.Connection) -> tuple[list[activity.Message], int, int]:
        messages = activity.recent_messages(conn, activity.MESSAGES_MAX)
        return messages, activity.message_total(conn), activity.messages_after(conn, seen_id)

    messages, total, new_count = await db_call(load)
    response = templates.TemplateResponse(
        request,
        "admin/messages.html",
        {
            "tab": "messages",
            "days": activity.group_by_day(messages),
            "shown": len(messages),
            "total": total,
            "new_count": new_count,
            "seen_id": seen_id,
        },
    )
    if messages and messages[0].message_id > seen_id:
        response.set_cookie(
            SEEN_COOKIE,
            str(messages[0].message_id),
            max_age=SEEN_COOKIE_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            secure=SESSION_COOKIE_SECURE,
            path="/admin",
        )
    return response


@router.get("/admin/messages/new-count", response_class=HTMLResponse)
async def new_message_count(request: Request) -> Response:
    """Return the count badge for the Messages tab, or nothing when there is nothing new."""
    require_admin(request)
    seen_id = seen_message_id(request.cookies.get(SEEN_COOKIE))
    count = await db_call(lambda conn: activity.messages_after(conn, seen_id))
    if count == 0:
        return HTMLResponse("")
    label = "99+" if count > 99 else str(count)
    return HTMLResponse(f'<span class="badge" aria-label="{count} new">{label}</span>')
