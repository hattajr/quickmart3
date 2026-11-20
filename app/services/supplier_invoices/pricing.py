"""Compare what suppliers charge with shop prices and apply the admin's price decisions."""

import re
import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process, utils

MARKUP_FLAG_PCT = 10
MARKUP_SUGGEST_PCT = 15
MARKUP_CHOICES_PCT = (5, 10, 15, 20, 30, 50)
PRICE_STEP_WON = 100
PRICE_WON_MAX = 10_000_000
MATCH_SCORE_MIN = 70
MATCH_SCORE_SURE = 90
MATCH_CANDIDATES_MAX = 5
SEARCH_RESULTS_MAX = 8
# A typed search hides shop products whose names share little with it.
SEARCH_SCORE_MIN = 60
SHOP_PRODUCTS_MAX = 20_000
# A row with no delivered amount gives no hint whether its price is per piece or per box; a price this
# many times the shop's selling price can only be a box.
BOX_GUESS_RATIO = 4
NOT_PRODUCTS = ("shipping", "택배", "배송비")


@dataclass(frozen=True)
class UnitCost:
    cost_won: int
    rule: str
    basis: str


@dataclass(frozen=True)
class ShopProduct:
    id: int
    name: str
    brand: str
    price: int
    unit: str
    purchase_price: int | None

    @property
    def label(self) -> str:
        """Return brand and name as the shop shows them."""
        name = self.name.strip()
        brand = self.brand.strip()
        return name if not brand or brand.lower() in name.lower() else f"{brand} {name}"


@dataclass(frozen=True)
class Review:
    action: str
    product_id: int | None
    old_price: int | None
    new_price: int | None


@dataclass
class PriceLine:
    item_id: int
    supplier_product_id: int
    name_latin: str
    product_name: str
    cost: UnitCost
    prev_cost_won: int | None
    shop: ShopProduct | None
    match_source: str
    match_score: float | None
    review: Review | None
    suggestions: list[tuple[int, int]] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Return fix, ok, no_match or not_sold."""
        if self.match_source == "not_sold":
            return "not_sold"
        if self.shop is None:
            return "no_match"
        return "fix" if needs_raise(self.shop.price, self.cost.cost_won) else "ok"

    @property
    def check_match(self) -> bool:
        """Return whether the shop product was guessed from a name that is only fairly alike."""
        return self.match_source == "fuzzy" and (self.match_score or 0) < MATCH_SCORE_SURE

    @property
    def needs_attention(self) -> bool:
        """Return whether the line is still waiting for an Update or Skip."""
        return self.review is None and self.status in ("fix", "no_match")

    @property
    def suggested_price(self) -> int:
        """Return the default new price, cost plus the suggested markup."""
        return suggest_price(self.cost.cost_won, MARKUP_SUGGEST_PCT)

    @property
    def markup_now_pct(self) -> int | None:
        """Return the current shop price's markup on cost, in percent."""
        if self.shop is None or self.cost.cost_won <= 0:
            return None
        return round((self.shop.price / self.cost.cost_won - 1) * 100)


@dataclass(frozen=True)
class InvoiceSummary:
    id: int
    supplier: str
    invoice_date: str
    invoice_no: str
    page_count: int
    total_qty: int
    total_amount: int
    item_count: int


@dataclass
class InvoiceReview:
    invoice: InvoiceSummary
    lines: list[PriceLine]

    @property
    def to_fix_count(self) -> int:
        """Return how many lines still need a decision."""
        return sum(1 for line in self.lines if line.needs_attention)

    @property
    def shown(self) -> list[PriceLine]:
        """Return lines listed up front: ones to fix and ones already decided."""
        return [line for line in self.lines if line.review is not None or line.status in ("fix", "no_match")]

    @property
    def folded(self) -> list[PriceLine]:
        """Return lines priced fine or not sold, listed behind one line."""
        return [line for line in self.lines if line.review is None and line.status in ("ok", "not_sold")]


def per_box_count(items_per_box: str) -> int:
    """Return the per-box count from text like '24 EA', or 1 when none is printed."""
    match = re.search(r"\d+", items_per_box)
    return int(match.group()) if match and int(match.group()) > 0 else 1


def unit_cost(unit_price: int, amount: int, items_per_box: str, note: str, shop_price: int | None) -> UnitCost:
    """Return the cost of one piece, deciding whether the printed unit price is per piece, pack or box."""
    if unit_price <= 0:
        raise ValueError(f"unit price must be positive, got {unit_price}")
    pack = re.search(r"quantity\s*:\s*(\d+)\s*pcs", note, re.IGNORECASE)
    if pack and int(pack.group(1)) > 1:
        pieces = int(pack.group(1))
        return UnitCost(round(unit_price / pieces), "pack", f"Pack of {pieces} at {unit_price:,}")
    per_box = per_box_count(items_per_box)
    if per_box <= 1:
        return UnitCost(unit_price, "piece", "Per piece")
    if amount > 0 and unit_price == amount:
        return UnitCost(round(unit_price / per_box), "box", f"Box of {per_box} at {unit_price:,}")
    if 0 < unit_price < amount:
        return UnitCost(unit_price, "piece", "Per piece")
    # Not delivered (amount 0), or a printed unit price above the amount: guess from the shop price.
    if shop_price and unit_price >= BOX_GUESS_RATIO * shop_price:
        return UnitCost(round(unit_price / per_box), "box", f"Box of {per_box} at {unit_price:,} (guessed)")
    return UnitCost(unit_price, "piece", "Per piece (guessed)")


def suggest_price(cost_won: int, markup_pct: int) -> int:
    """Return cost plus markup, rounded up to the next price step."""
    if cost_won <= 0 or not 0 <= markup_pct <= 500:
        raise ValueError(f"bad cost {cost_won} or markup {markup_pct}")
    step = 100 * PRICE_STEP_WON
    return (cost_won * (100 + markup_pct) + step - 1) // step * PRICE_STEP_WON


def needs_raise(shop_price: int, cost_won: int) -> bool:
    """Return whether the shop price is below cost plus the flag markup."""
    return shop_price * 100 < cost_won * (100 + MARKUP_FLAG_PCT)


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, and split sizes like '350g' into '350 g' for matching."""
    return re.sub(r"(\d)([a-z])", r"\1 \2", utils.default_process(text))


def is_shipping(name_latin: str, product_name: str) -> bool:
    """Return whether an invoice line is a delivery fee rather than a product."""
    text = f"{name_latin} {product_name}".lower()
    return any(word in text for word in NOT_PRODUCTS)


def load_shop(conn: sqlite3.Connection) -> list[ShopProduct]:
    """Return every shop product."""
    rows = conn.execute(
        "SELECT id, name, coalesce(brand, ''), price, coalesce(unit, ''), purchase_price FROM products"
        " ORDER BY id LIMIT ?",
        (SHOP_PRODUCTS_MAX,),
    ).fetchall()
    return [ShopProduct(int(r[0]), str(r[1]), str(r[2]), int(r[3]), str(r[4]), r[5]) for r in rows]


def shop_choices(shop: list[ShopProduct]) -> dict[int, str]:
    """Return each shop product's normalized brand and name, keyed by list position."""
    return {index: normalize(f"{p.brand} {p.name}") for index, p in enumerate(shop)}


def rank_products(
    query: str, shop: list[ShopProduct], limit: int, choices: dict[int, str] | None = None
) -> list[tuple[ShopProduct, float]]:
    """Return the shop products whose names are most like the query, best first."""
    if not 1 <= limit <= 50:
        raise ValueError(f"limit must be 1 to 50, got {limit}")
    wanted = normalize(query)
    if not wanted:
        return []
    choices = choices if choices is not None else shop_choices(shop)
    found = process.extract(wanted, choices, scorer=fuzz.token_set_ratio, limit=limit)
    return [(shop[index], round(float(score), 1)) for _, score, index in found]


_ITEM_SQL = (
    "SELECT i.id, i.product_id, p.name_latin, i.product_name, i.items_per_box, i.unit_price, i.amount, i.note,"
    " m.supplier_product_id IS NOT NULL, m.product_id,"
    " r.action, r.product_id, r.old_price, r.new_price"
    " FROM supplier_invoice_items i"
    " JOIN supplier_products p ON p.id = i.product_id"
    " LEFT JOIN supplier_product_matches m ON m.supplier_product_id = i.product_id"
    " LEFT JOIN supplier_price_reviews r ON r.item_id = i.id"
    " WHERE i.invoice_id = ? AND i.struck_out = 0 AND i.unit_price > 0"
    " ORDER BY i.page_no, i.line_no"
)


def _summary(conn: sqlite3.Connection, invoice_id: int) -> InvoiceSummary | None:
    row = conn.execute(
        "SELECT v.id, v.supplier, v.invoice_date, v.invoice_no, v.page_count, v.total_qty, v.total_amount,"
        " (SELECT count(*) FROM supplier_invoice_items i WHERE i.invoice_id = v.id AND i.struck_out = 0)"
        " FROM supplier_invoices v WHERE v.id = ?",
        (invoice_id,),
    ).fetchone()
    if row is None:
        return None
    return InvoiceSummary(
        int(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), int(row[5]), int(row[6]), int(row[7])
    )


def _previous_rows(conn: sqlite3.Connection, invoice: InvoiceSummary) -> dict[int, tuple[int, int, str, str]]:
    """Return each supplier product's last priced row before this invoice."""
    rows = conn.execute(
        "SELECT i.product_id, i.unit_price, i.amount, i.items_per_box, i.note"
        " FROM supplier_invoice_items i JOIN supplier_invoices v ON v.id = i.invoice_id"
        " WHERE v.supplier = ? AND v.invoice_date < ? AND i.struck_out = 0 AND i.unit_price > 0"
        " ORDER BY v.invoice_date, i.id",
        (invoice.supplier, invoice.invoice_date),
    ).fetchall()
    return {int(r[0]): (int(r[1]), int(r[2]), str(r[3]), str(r[4])) for r in rows}


def _match(
    name_latin: str, has_saved: bool, saved_product_id: int | None, shop: list[ShopProduct], choices: dict[int, str]
) -> tuple[ShopProduct | None, str, float | None]:
    """Return the shop product for a supplier product, how it was found, and the name score."""
    if has_saved:
        if saved_product_id is None:
            return None, "not_sold", None
        by_id = next((p for p in shop if p.id == saved_product_id), None)
        if by_id is not None:
            return by_id, "saved", None
    ranked = rank_products(name_latin, shop, 1, choices)
    if ranked and ranked[0][1] >= MATCH_SCORE_MIN:
        return ranked[0][0], "fuzzy", ranked[0][1]
    return None, "none", ranked[0][1] if ranked else None


def build_review(
    conn: sqlite3.Connection, invoice_id: int, shop: list[ShopProduct] | None = None
) -> InvoiceReview | None:
    """Return an invoice with a price check for every priced product line, or None if it does not exist."""
    invoice = _summary(conn, invoice_id)
    if invoice is None:
        return None
    shop = shop if shop is not None else load_shop(conn)
    previous = _previous_rows(conn, invoice)
    choices = shop_choices(shop)
    lines: list[PriceLine] = []
    for found in conn.execute(_ITEM_SQL, (invoice_id,)).fetchall():
        row = tuple(found)
        item_id, product_id, name_latin, product_name, per_box, unit_price, amount, note = row[:8]
        has_saved, saved_id, action, review_product, old_price, new_price = row[8:]
        if is_shipping(str(name_latin), str(product_name)):
            continue
        shop_product, source, score = _match(str(name_latin), bool(has_saved), saved_id, shop, choices)
        shop_price = shop_product.price if shop_product else None
        cost = unit_cost(int(unit_price), int(amount), str(per_box), str(note), shop_price)
        prev = previous.get(int(product_id))
        prev_cost = unit_cost(prev[0], prev[1], prev[2], prev[3], shop_price).cost_won if prev else None
        review = None if action is None else Review(str(action), review_product, old_price, new_price)
        lines.append(
            PriceLine(
                item_id=int(item_id),
                supplier_product_id=int(product_id),
                name_latin=str(name_latin),
                product_name=str(product_name),
                cost=cost,
                prev_cost_won=prev_cost,
                shop=shop_product,
                match_source=source,
                match_score=score,
                review=review,
                suggestions=[(pct, suggest_price(cost.cost_won, pct)) for pct in MARKUP_CHOICES_PCT],
            )
        )
    return InvoiceReview(invoice, lines)


def recent_invoices(conn: sqlite3.Connection, limit: int = 20) -> list[InvoiceReview]:
    """Return the newest invoices with their price checks."""
    if not 1 <= limit <= 100:
        raise ValueError(f"limit must be 1 to 100, got {limit}")
    shop = load_shop(conn)
    ids = conn.execute(
        "SELECT id FROM supplier_invoices ORDER BY invoice_date DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    reviews = [build_review(conn, int(row[0]), shop) for row in ids]
    return [review for review in reviews if review is not None]


def invoice_id_of_item(conn: sqlite3.Connection, item_id: int) -> int | None:
    """Return the invoice an item belongs to."""
    row = conn.execute("SELECT invoice_id FROM supplier_invoice_items WHERE id = ?", (item_id,)).fetchone()
    return None if row is None else int(row[0])


def price_line(conn: sqlite3.Connection, item_id: int) -> PriceLine | None:
    """Return the price check for one invoice line."""
    invoice_id = invoice_id_of_item(conn, item_id)
    review = None if invoice_id is None else build_review(conn, invoice_id)
    if review is None:
        return None
    return next((line for line in review.lines if line.item_id == item_id), None)


class ReviewError(Exception):
    """A price decision that cannot be applied, with a message for the admin."""


def _save_match(conn: sqlite3.Connection, supplier_product_id: int, product_id: int | None) -> None:
    conn.execute(
        "INSERT INTO supplier_product_matches (supplier_product_id, product_id) VALUES (?, ?)"
        " ON CONFLICT (supplier_product_id) DO UPDATE SET product_id = excluded.product_id,"
        " updated_at = CURRENT_TIMESTAMP",
        (supplier_product_id, product_id),
    )


def apply_update(conn: sqlite3.Connection, item_id: int, product_id: int, price_won: int) -> None:
    """Set the shop price and cost from one invoice line, remember the match, and record it for Undo."""
    if not 0 < price_won <= PRICE_WON_MAX:
        raise ReviewError(f"Type a price from 1 to {PRICE_WON_MAX:,} won.")
    line = price_line(conn, item_id)
    if line is None:
        raise ReviewError("This invoice line no longer exists.")
    if line.review is not None:
        raise ReviewError("This line was already handled. Undo it first.")
    product = conn.execute("SELECT price, purchase_price FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        raise ReviewError("That shop product no longer exists.")
    old_price, old_purchase = int(product[0]), product[1]
    cost = unit_cost_for(conn, item_id, old_price)
    with conn:
        conn.execute(
            "UPDATE products SET price = ?, purchase_price = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (price_won, cost, product_id),
        )
        conn.execute(
            "INSERT INTO supplier_price_reviews"
            " (item_id, action, product_id, old_price, new_price, old_purchase_price, new_purchase_price)"
            " VALUES (?, 'updated', ?, ?, ?, ?, ?)",
            (item_id, product_id, old_price, price_won, old_purchase, cost),
        )
        _save_match(conn, line.supplier_product_id, product_id)


def unit_cost_for(conn: sqlite3.Connection, item_id: int, shop_price: int | None) -> int:
    """Return one line's cost per piece, judged against a shop price."""
    row = conn.execute(
        "SELECT unit_price, amount, items_per_box, note FROM supplier_invoice_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise ReviewError("This invoice line no longer exists.")
    return unit_cost(int(row[0]), int(row[1]), str(row[2]), str(row[3]), shop_price).cost_won


def apply_skip(conn: sqlite3.Connection, item_id: int) -> None:
    """Record that the admin left this line's shop price as it is."""
    line = price_line(conn, item_id)
    if line is None:
        raise ReviewError("This invoice line no longer exists.")
    if line.review is not None:
        raise ReviewError("This line was already handled. Undo it first.")
    with conn:
        conn.execute(
            "INSERT INTO supplier_price_reviews (item_id, action, product_id) VALUES (?, 'skipped', ?)",
            (item_id, line.shop.id if line.shop else None),
        )


def undo_review(conn: sqlite3.Connection, item_id: int) -> None:
    """Undo an Update or Skip; an update is reverted only if nobody changed that price since."""
    row = conn.execute(
        "SELECT action, product_id, old_price, new_price, old_purchase_price FROM supplier_price_reviews"
        " WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ReviewError("There is nothing to undo on this line.")
    action, product_id, old_price, new_price, old_purchase = row
    with conn:
        if action == "updated" and product_id is not None:
            cursor = conn.execute(
                "UPDATE products SET price = ?, purchase_price = ?, updated_at = CURRENT_TIMESTAMP"
                " WHERE id = ? AND price = ?",
                (old_price, old_purchase, product_id, new_price),
            )
            if cursor.rowcount != 1:
                raise ReviewError("The price was changed again after this update, so it was left as it is.")
        conn.execute("DELETE FROM supplier_price_reviews WHERE item_id = ?", (item_id,))


def set_match(conn: sqlite3.Connection, item_id: int, product_id: int | None) -> None:
    """Remember which shop product this line's supplier product is, or that the shop does not sell it."""
    row = conn.execute("SELECT product_id FROM supplier_invoice_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ReviewError("This invoice line no longer exists.")
    if product_id is not None and conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone() is None:
        raise ReviewError("That shop product no longer exists.")
    with conn:
        _save_match(conn, int(row[0]), product_id)


def page_image(conn: sqlite3.Connection, invoice_id: int, page_no: int) -> bytes | None:
    """Return one stored invoice photo."""
    row = conn.execute(
        "SELECT image_jpeg FROM supplier_invoice_pages WHERE invoice_id = ? AND page_no = ?", (invoice_id, page_no)
    ).fetchone()
    return None if row is None else bytes(row[0])


def page_numbers(conn: sqlite3.Connection, invoice_id: int) -> list[int]:
    """Return the page numbers stored for an invoice."""
    rows = conn.execute(
        "SELECT page_no FROM supplier_invoice_pages WHERE invoice_id = ? ORDER BY page_no", (invoice_id,)
    ).fetchall()
    return [int(r[0]) for r in rows]
