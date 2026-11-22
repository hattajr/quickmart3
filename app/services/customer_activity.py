"""What customers do in the shop app: the products they add to the cart and the messages they send.

A tap is a row in search_selections: the shop records one each time a customer taps a product in
search results or in the favourites chips, which puts it in their cart.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# Korea has no daylight saving time, so a fixed offset is exact and needs no time zone database.
SHOP_UTC_OFFSET_HOURS = 9
SHOP_TZ = timezone(timedelta(hours=SHOP_UTC_OFFSET_HOURS), "KST")
PERIOD_DAYS_ALLOWED = (7, 14, 30)
POPULAR_PRODUCTS_MAX = 500
MESSAGES_MAX = 500
CHART_WIDTH_PX = 320
CHART_HEIGHT_PX = 130
_DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_SHOP_DAY_MODIFIER = f"+{SHOP_UTC_OFFSET_HOURS} hours"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# A tap without a session can't be tied to anyone, so each such tap counts as its own person.
_PERSON = "CASE WHEN s.session_id IS NULL OR s.session_id IN ('', 'unknown') THEN 'tap-' || s.id ELSE s.session_id END"


@dataclass(frozen=True)
class Period:
    days: int
    start_utc: datetime
    now_utc: datetime

    @property
    def before_start_utc(self) -> datetime:
        return self.start_utc - timedelta(days=self.days)

    @property
    def first_day(self) -> date:
        return to_shop_time(self.start_utc).date()

    @property
    def last_day(self) -> date:
        return to_shop_time(self.now_utc).date()


@dataclass(frozen=True)
class PopularProduct:
    product_id: int
    name: str
    price_won: int | None  # None when the product has since been deleted
    taps: int
    people: int
    taps_before: int
    people_before: int
    sold_pcs: int

    @property
    def exists(self) -> bool:
        return self.price_won is not None

    @property
    def is_new(self) -> bool:
        return self.taps_before == 0

    @property
    def change(self) -> int:
        return self.taps - self.taps_before


@dataclass(frozen=True)
class PopularReport:
    period: Period
    taps: int
    people: int
    product_count: int
    products: list[PopularProduct]


@dataclass(frozen=True)
class DayCount:
    day: date
    taps: int


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    width: float
    height: float
    taps: int
    center_x: float


@dataclass(frozen=True)
class Tick:
    x: float
    label: str
    anchor: str  # SVG text-anchor: start, middle or end


@dataclass(frozen=True)
class BarChart:
    width: int
    height: int
    base_y: float
    top_y: float
    max_taps: int
    bars: list[Bar]
    ticks: list[Tick]
    show_values: bool


@dataclass(frozen=True)
class Message:
    message_id: int
    text: str
    sent_at: datetime  # shop time
    device: str


@dataclass(frozen=True)
class MessageDay:
    day: date
    messages: list[Message]


def period_ending(days: int, now_utc: datetime) -> Period:
    """Return the last `days` days up to now."""
    if days not in PERIOD_DAYS_ALLOWED:
        raise ValueError(f"days must be one of {PERIOD_DAYS_ALLOWED}, got {days}")
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must carry a time zone")
    now = now_utc.astimezone(timezone.utc)
    return Period(days=days, start_utc=now - timedelta(days=days), now_utc=now)


def to_shop_time(moment: datetime) -> datetime:
    """Convert an aware time to shop time."""
    if moment.tzinfo is None:
        raise ValueError("moment must carry a time zone")
    return moment.astimezone(SHOP_TZ)


def from_db_time(text: str) -> datetime:
    """Read a CURRENT_TIMESTAMP value, which SQLite writes in UTC."""
    return datetime.strptime(text, _DB_TIME_FORMAT).replace(tzinfo=timezone.utc)


def _db_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_DB_TIME_FORMAT)


def _product_rows(conn: sqlite3.Connection, period: Period, product_id: int | None, limit: int) -> list[PopularProduct]:
    rows = conn.execute(
        f"""
        WITH now_taps AS (
            SELECT s.product_id, COUNT(*) AS taps, COUNT(DISTINCT {_PERSON}) AS people, MAX(s.product_name) AS logged_name
            FROM search_selections s
            WHERE s.selected_at >= ? AND (? IS NULL OR s.product_id = ?)
            GROUP BY s.product_id),
        before_taps AS (
            SELECT s.product_id, COUNT(*) AS taps, COUNT(DISTINCT {_PERSON}) AS people
            FROM search_selections s
            WHERE s.selected_at >= ? AND s.selected_at < ? AND (? IS NULL OR s.product_id = ?)
            GROUP BY s.product_id),
        sold AS (
            SELECT item_id, SUM(quantity) AS pcs FROM sold_items
            WHERE timestamp >= ? AND (? IS NULL OR item_id = ?)
            GROUP BY item_id)
        SELECT n.product_id, coalesce(p.name, n.logged_name, 'Deleted product') AS name, p.price,
               n.taps, n.people, coalesce(b.taps, 0) AS taps_before, coalesce(b.people, 0) AS people_before,
               coalesce(sold.pcs, 0) AS sold_pcs
        FROM now_taps n
        LEFT JOIN products p ON p.id = n.product_id
        LEFT JOIN before_taps b ON b.product_id = n.product_id
        LEFT JOIN sold ON sold.item_id = n.product_id
        ORDER BY n.taps DESC, n.people DESC, name COLLATE NOCASE
        LIMIT ?
        """,
        (
            _db_time(period.start_utc), product_id, product_id,
            _db_time(period.before_start_utc), _db_time(period.start_utc), product_id, product_id,
            _db_time(period.start_utc), product_id, product_id,
            limit,
        ),
    ).fetchall()  # fmt: skip
    return [
        PopularProduct(
            product_id=int(r[0]),
            name=str(r[1]).strip(),
            price_won=None if r[2] is None else int(r[2]),
            taps=int(r[3]),
            people=int(r[4]),
            taps_before=int(r[5]),
            people_before=int(r[6]),
            sold_pcs=int(r[7]),
        )
        for r in rows
    ]


def popular_products(conn: sqlite3.Connection, period: Period, limit: int) -> PopularReport:
    """Rank the products customers added to the cart in the period, most taps first."""
    if not 1 <= limit <= POPULAR_PRODUCTS_MAX:
        raise ValueError(f"limit must be 1 to {POPULAR_PRODUCTS_MAX}, got {limit}")
    total = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT {_PERSON}), COUNT(DISTINCT s.product_id)"
        " FROM search_selections s WHERE s.selected_at >= ?",
        (_db_time(period.start_utc),),
    ).fetchone()
    products = _product_rows(conn, period, None, limit)
    return PopularReport(
        period=period, taps=int(total[0]), people=int(total[1]), product_count=int(total[2]), products=products
    )


def product_numbers(conn: sqlite3.Connection, period: Period, product_id: int) -> PopularProduct | None:
    """Return one product's numbers for the period; None when it has no taps and no longer exists."""
    rows = _product_rows(conn, period, product_id, 1)
    if rows:
        return rows[0]
    shop = conn.execute("SELECT name, price FROM products WHERE id = ?", (product_id,)).fetchone()
    if shop is None:
        return None
    sold = conn.execute(
        "SELECT coalesce(SUM(quantity), 0) FROM sold_items WHERE item_id = ? AND timestamp >= ?",
        (product_id, _db_time(period.start_utc)),
    ).fetchone()
    taps_before = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT {_PERSON}) FROM search_selections s"
        " WHERE s.product_id = ? AND s.selected_at >= ? AND s.selected_at < ?",
        (product_id, _db_time(period.before_start_utc), _db_time(period.start_utc)),
    ).fetchone()
    return PopularProduct(
        product_id=product_id,
        name=str(shop[0]).strip(),
        price_won=int(shop[1]),
        taps=0,
        people=0,
        taps_before=int(taps_before[0]),
        people_before=int(taps_before[1]),
        sold_pcs=int(sold[0]),
    )


def daily_taps(conn: sqlite3.Connection, period: Period, product_id: int) -> list[DayCount]:
    """Count one product's taps per shop day, from the period's first day to today."""
    counts = dict(
        conn.execute(
            "SELECT date(selected_at, ?) AS day, COUNT(*) FROM search_selections"
            " WHERE product_id = ? AND selected_at >= ? GROUP BY day",
            (_SHOP_DAY_MODIFIER, product_id, _db_time(period.start_utc)),
        ).fetchall()
    )
    day_count = (period.last_day - period.first_day).days + 1
    if not 1 <= day_count <= max(PERIOD_DAYS_ALLOWED) + 1:
        raise ValueError(f"period spans {day_count} shop days")
    days = [period.first_day + timedelta(days=i) for i in range(day_count)]
    return [DayCount(day=d, taps=int(counts.get(d.isoformat(), 0))) for d in days]


def bar_chart(days: list[DayCount]) -> BarChart:
    """Lay out one bar per day on a fixed-size chart."""
    if not days:
        raise ValueError("a chart needs at least one day")
    left_px, right_px, top_px, bottom_px = 18.0, 2.0, 14.0, 22.0
    width, height = CHART_WIDTH_PX, CHART_HEIGHT_PX
    max_taps = max(max(d.taps for d in days), 1)
    plot_height = height - top_px - bottom_px
    slot = (width - left_px - right_px) / len(days)
    base_y = top_px + plot_height

    def y_of(taps: int) -> float:
        return top_px + plot_height * (1 - taps / max_taps)

    bars: list[Bar] = []
    for i, d in enumerate(days):
        slot_x = left_px + i * slot
        bars.append(
            Bar(
                x=round(slot_x + slot * 0.15, 1),
                y=round(y_of(d.taps), 1),
                width=round(slot * 0.7, 1),
                height=round(base_y - y_of(d.taps), 1),
                taps=d.taps,
                center_x=round(slot_x + slot / 2, 1),
            )
        )
    last = len(days) - 1
    ticks: list[Tick] = []
    if len(days) <= 8:
        for i in range(len(days)):
            ticks.append(Tick(x=bars[i].center_x, label=_day_month(days[i].day), anchor="middle"))
    else:
        # Narrow slots: the end labels hug the chart's edges so they aren't cut off.
        for i in sorted({0, last // 3, 2 * last // 3, last}):
            if i == 0:
                x, anchor = left_px, "start"
            elif i == last:
                x, anchor = width - right_px, "end"
            else:
                x, anchor = bars[i].center_x, "middle"
            ticks.append(Tick(x=x, label=_day_month(days[i].day), anchor=anchor))
    return BarChart(
        width=width,
        height=height,
        base_y=round(base_y, 1),
        top_y=round(top_px, 1),
        max_taps=max_taps,
        bars=bars,
        ticks=ticks,
        show_values=len(days) <= 15,
    )


def _day_month(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def device_name(user_agent: str | None) -> str:
    """Name the kind of device a browser's user agent describes."""
    agent = user_agent or ""
    for marker, name in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Macintosh", "Mac"),
        ("Windows", "Windows"),
        ("CrOS", "Chromebook"),
        ("Linux", "Linux"),
    ):
        if marker in agent:
            return name
    return "Unknown device"


def recent_messages(conn: sqlite3.Connection, limit: int) -> list[Message]:
    """Return the messages customers sent and were not hidden, newest first."""
    if not 1 <= limit <= MESSAGES_MAX:
        raise ValueError(f"limit must be 1 to {MESSAGES_MAX}, got {limit}")
    rows = conn.execute(
        "SELECT id, message, created_at, user_agent FROM feedback_messages"
        " WHERE is_deleted = 'N' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        Message(
            message_id=int(r[0]),
            text=str(r[1]),
            sent_at=to_shop_time(from_db_time(str(r[2]))),
            device=device_name(r[3]),
        )
        for r in rows
    ]


def message_total(conn: sqlite3.Connection) -> int:
    """Count the messages that were not hidden."""
    return int(conn.execute("SELECT COUNT(*) FROM feedback_messages WHERE is_deleted = 'N'").fetchone()[0])


def messages_after(conn: sqlite3.Connection, message_id: int) -> int:
    """Count the messages newer than the given one."""
    row = conn.execute(
        "SELECT COUNT(*) FROM feedback_messages WHERE is_deleted = 'N' AND id > ?", (message_id,)
    ).fetchone()
    return int(row[0])


def group_by_day(messages: list[Message]) -> list[MessageDay]:
    """Group newest-first messages by shop day, keeping their order."""
    groups: list[MessageDay] = []
    for message in messages:
        day = message.sent_at.date()
        if groups and groups[-1].day == day:
            groups[-1].messages.append(message)
        else:
            groups.append(MessageDay(day=day, messages=[message]))
    return groups


def day_label(day: date, this_year: int) -> str:
    """Write a day as 'Sun, 1 Nov', adding the year when it isn't this year."""
    label = f"{_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month - 1]}"
    return label if day.year == this_year else f"{label} {day.year}"


def range_label(first: date, last: date) -> str:
    """Write a date range as '15 – 22 Nov' or '23 Oct – 22 Nov'."""
    if last < first:
        raise ValueError(f"range ends ({last}) before it starts ({first})")
    end = f"{last.day} {_MONTHS[last.month - 1]}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day} – {end}"
    return f"{first.day} {_MONTHS[first.month - 1]} – {end}"
