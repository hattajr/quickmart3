"""Tests for the admin's customer activity: popular products, taps per day, and messages."""

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services import customer_activity as activity  # noqa: E402
from app.services.customer_activity import DayCount  # noqa: E402
from app.services.supplier_invoices.ledger import apply_schema  # noqa: E402

NOW = datetime(2026, 11, 22, 4, 0, 0, tzinfo=timezone.utc)  # 13:00 in Korea


def ago(days: float = 0, hours: float = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON;")
    apply_schema(connection)
    connection.executemany(
        "INSERT INTO products (id, name, price) VALUES (?, ?, ?)",
        [(1, "Indomie mie goreng", 800), (2, "SilverQueen", 3500), (3, "Frozen tempe", 2500)],
    )
    taps = [
        # Indomie: 3 taps from 2 people this week, 1 the week before.
        ("s1", 1, "Indomie mie goreng", ago(1)),
        ("s1", 1, "Indomie mie goreng", ago(1, 1)),
        ("s2", 1, "Indomie mie goreng", ago(2)),
        ("s3", 1, "Indomie mie goreng", ago(9)),
        # SilverQueen: 3 taps with no session, each its own person; 4 the week before.
        ("unknown", 2, "SilverQueen", ago(3)),
        ("unknown", 2, "SilverQueen", ago(3, 1)),
        (None, 2, "SilverQueen", ago(4)),
        *[("s4", 2, "SilverQueen", ago(10)) for _ in range(4)],
        # A deleted product keeps the name the tap logged.
        ("s5", 99, "Old chips", ago(5)),
        # Too old for 14 days.
        ("s6", 3, "Frozen tempe", ago(20)),
    ]
    connection.executemany(
        "INSERT INTO search_selections (session_id, product_id, product_name, search_query, selected_at)"
        " VALUES (?, ?, ?, '', ?)",
        taps,
    )
    connection.executemany(
        "INSERT INTO sold_items (session_id, item_id, item_name, price_at_purchase, quantity, total_price, timestamp)"
        " VALUES ('s1', ?, ?, ?, ?, ?, ?)",
        [(1, "Indomie mie goreng", 800, 5, 4000, ago(1)), (1, "Indomie mie goreng", 800, 2, 1600, ago(12))],
    )
    return connection


def week(conn: sqlite3.Connection) -> activity.PopularReport:
    return activity.popular_products(conn, activity.period_ending(7, NOW), 10)


def test_period_ending_counts_back_from_now() -> None:
    period = activity.period_ending(7, NOW)
    assert period.start_utc == NOW - timedelta(days=7)
    assert period.before_start_utc == NOW - timedelta(days=14)
    assert (period.first_day, period.last_day) == (date(2026, 11, 15), date(2026, 11, 22))


def test_period_ending_rejects_other_days_and_naive_times() -> None:
    with pytest.raises(ValueError, match="days"):
        activity.period_ending(10, NOW)
    with pytest.raises(ValueError, match="time zone"):
        activity.period_ending(7, NOW.replace(tzinfo=None))


def test_popular_ranks_by_taps_and_counts_people(conn: sqlite3.Connection) -> None:
    report = week(conn)
    # Both have 3 taps; SilverQueen has more people, so it comes first.
    assert [p.name for p in report.products] == ["SilverQueen", "Indomie mie goreng", "Old chips"]
    silverqueen, indomie, chips = report.products
    assert (indomie.taps, indomie.people, indomie.taps_before, indomie.sold_pcs) == (3, 2, 1, 5)
    assert indomie.change == 2 and not indomie.is_new
    # Taps with no session can't be tied to one person, so each counts once.
    assert (silverqueen.taps, silverqueen.people, silverqueen.taps_before) == (3, 3, 4)
    assert silverqueen.change == -1
    assert chips.is_new and not chips.exists and chips.price_won is None
    assert (report.taps, report.people, report.product_count) == (7, 6, 3)


def test_popular_ties_on_taps_and_people_go_by_name(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM search_selections WHERE product_id = 1 AND selected_at = ?", (ago(1, 1),))
    conn.execute("DELETE FROM search_selections WHERE product_id = 2 AND selected_at = ?", (ago(4),))
    report = week(conn)
    # Now both have 2 taps from 2 people.
    assert [(p.name, p.taps, p.people) for p in report.products[:2]] == [
        ("Indomie mie goreng", 2, 2),
        ("SilverQueen", 2, 2),
    ]


def test_popular_respects_limit_and_rejects_bad_limits(conn: sqlite3.Connection) -> None:
    period = activity.period_ending(7, NOW)
    report = activity.popular_products(conn, period, 1)
    assert len(report.products) == 1 and report.product_count == 3
    for bad in (0, activity.POPULAR_PRODUCTS_MAX + 1):
        with pytest.raises(ValueError, match="limit"):
            activity.popular_products(conn, period, bad)


def test_popular_on_an_empty_shop_has_no_products() -> None:
    empty = sqlite3.connect(":memory:")
    apply_schema(empty)
    report = activity.popular_products(empty, activity.period_ending(30, NOW), 10)
    assert (report.products, report.taps, report.people, report.product_count) == ([], 0, 0, 0)


def test_product_numbers_for_tapped_untapped_and_missing(conn: sqlite3.Connection) -> None:
    period = activity.period_ending(7, NOW)
    tapped = activity.product_numbers(conn, period, 1)
    assert tapped is not None and (tapped.taps, tapped.sold_pcs) == (3, 5)
    untapped = activity.product_numbers(conn, period, 3)
    assert untapped is not None and (untapped.taps, untapped.people, untapped.price_won) == (0, 0, 2500)
    assert activity.product_numbers(conn, period, 12345) is None


def test_daily_taps_buckets_by_korean_day(conn: sqlite3.Connection) -> None:
    # 16:30 UTC on 20 Nov is 01:30 on 21 Nov in Korea.
    conn.execute(
        "INSERT INTO search_selections (session_id, product_id, product_name, search_query, selected_at)"
        " VALUES ('s8', 3, 'Frozen tempe', '', '2026-11-20 16:30:00')"
    )
    days = activity.daily_taps(conn, activity.period_ending(7, NOW), 3)
    assert len(days) == 8
    assert days[0].day == date(2026, 11, 15) and days[-1].day == date(2026, 11, 22)
    assert {d.day: d.taps for d in days if d.taps} == {date(2026, 11, 21): 1}


def test_bar_chart_scales_to_the_busiest_day() -> None:
    days = [DayCount(date(2026, 11, 15) + timedelta(days=i), taps) for i, taps in enumerate([0, 2, 4, 1])]
    chart = activity.bar_chart(days)
    assert chart.max_taps == 4 and chart.show_values
    tallest = chart.bars[2]
    assert tallest.y == chart.top_y and tallest.y + tallest.height == chart.base_y
    assert chart.bars[0].height == 0
    assert [t.label for t in chart.ticks] == ["15 Nov", "16 Nov", "17 Nov", "18 Nov"]


def test_bar_chart_thins_ticks_for_a_month_and_needs_a_day() -> None:
    month = [DayCount(date(2026, 10, 23) + timedelta(days=i), 0) for i in range(31)]
    chart = activity.bar_chart(month)
    assert chart.max_taps == 1 and not chart.show_values
    assert [t.label for t in chart.ticks] == ["23 Oct", "2 Nov", "12 Nov", "22 Nov"]
    assert [t.anchor for t in chart.ticks] == ["start", "middle", "middle", "end"]
    assert chart.ticks[-1].x <= chart.width
    with pytest.raises(ValueError):
        activity.bar_chart([])


def test_device_name() -> None:
    assert activity.device_name("Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X)") == "iPhone"
    assert activity.device_name("Mozilla/5.0 (Linux; Android 10; K) Mobile Safari") == "Android"
    assert activity.device_name("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)") == "Mac"
    assert activity.device_name(None) == "Unknown device"
    assert activity.device_name("curl/8.0") == "Unknown device"


def add_message(conn: sqlite3.Connection, text: str, created_at: str, hidden: bool = False) -> int:
    cursor = conn.execute(
        "INSERT INTO feedback_messages (session_id, message, user_agent, created_at, is_deleted)"
        " VALUES ('s1', ?, 'Mozilla/5.0 (Linux; Android 10; K)', ?, ?)",
        (text, created_at, "Y" if hidden else "N"),
    )
    if cursor.lastrowid is None:
        raise AssertionError("insert gave no id")
    return cursor.lastrowid


def test_messages_skip_hidden_and_come_newest_first_in_korean_time(conn: sqlite3.Connection) -> None:
    first = add_message(conn, "Kerupuk", "2026-09-25 21:53:59")
    add_message(conn, "testss", "2026-10-01 00:00:00", hidden=True)
    last = add_message(conn, "Is there any chicken.", "2026-11-01 08:36:46")
    messages = activity.recent_messages(conn, 10)
    assert [m.message_id for m in messages] == [last, first]
    assert messages[1].sent_at == datetime(2026, 9, 26, 6, 53, 59, tzinfo=activity.SHOP_TZ)
    assert messages[0].device == "Android"
    assert activity.message_total(conn) == 2
    assert activity.messages_after(conn, first) == 1
    assert activity.messages_after(conn, 0) == 2


def test_recent_messages_rejects_bad_limits(conn: sqlite3.Connection) -> None:
    for bad in (0, activity.MESSAGES_MAX + 1):
        with pytest.raises(ValueError, match="limit"):
            activity.recent_messages(conn, bad)


def test_group_by_day_keeps_order(conn: sqlite3.Connection) -> None:
    add_message(conn, "a", "2026-11-01 01:00:00")
    add_message(conn, "b", "2026-11-01 08:00:00")
    add_message(conn, "c", "2026-11-01 16:00:00")  # 2 Nov in Korea
    groups = activity.group_by_day(activity.recent_messages(conn, 10))
    assert [(g.day.day, [m.text for m in g.messages]) for g in groups] == [(2, ["c"]), (1, ["b", "a"])]
    assert activity.group_by_day([]) == []


def test_day_and_range_labels() -> None:
    assert activity.day_label(date(2026, 11, 1), 2026) == "Sun, 1 Nov"
    assert activity.day_label(date(2025, 12, 12), 2026) == "Fri, 12 Dec 2025"
    assert activity.range_label(date(2026, 11, 15), date(2026, 11, 22)) == "15 – 22 Nov"
    assert activity.range_label(date(2026, 10, 23), date(2026, 11, 22)) == "23 Oct – 22 Nov"
    with pytest.raises(ValueError):
        activity.range_label(date(2026, 11, 22), date(2026, 11, 15))
