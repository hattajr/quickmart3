"""Tests for supplier price checks: unit costs, suggestions, matching and price decisions."""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.supplier_invoices import pricing  # noqa: E402
from app.services.supplier_invoices.ledger import apply_schema, build_invoices, store_invoice  # noqa: E402
from app.services.supplier_invoices.parse import Item, Page  # noqa: E402
from app.services.supplier_invoices.pricing import ReviewError  # noqa: E402


def item(name: str, qty: int, unit_price: int, amount: int, per_box: str = "", note: str = "") -> Item:
    return Item(name, name, per_box, "", qty, unit_price, amount, note, False)


def page(name: str, invoice_date: str, items: list[Item]) -> Page:
    qty, amount = sum(i.qty for i in items), sum(i.amount for i in items)
    return Page(name, f"sha-{name}", 0, b"jpeg", 1, 1, "{}", "하나피아", "거래명세표", "1", invoice_date, 1, 1, qty, amount, items)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON;")
    apply_schema(connection)
    connection.executemany(
        "INSERT INTO products (id, name, brand, price, unit) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Hydro coco original", "Hydro Coco", 1500, "330ml"),
            (2, "Indomie mie goreng", "Indomie", 800, "pcs"),
            (3, "Kacang sukro dua kelinci", "", 2000, "95g"),
        ],
    )
    older = page("a.jpg", "2026-11-05", [item("Hydro Coco 330ml", 1, 36000, 36000, "24 EA")])
    newer = page(
        "b.jpg",
        "2026-11-19",
        [
            item("Hydro Coco 330ml", 1, 37008, 37008, "24 EA"),
            item("Indomie Mi Goreng", 2, 750, 1500),
            item("Shipping Fee", 1, 4000, 4000),
            item("Zzyzx Qwerty Snack 100g", 1, 2900, 2900),
            item("Dua Kelinci Sukro 95g", 1, 1800, 1800),
        ],
    )
    for built in (older, newer):
        invoices, failures = build_invoices([built])
        assert failures == []
        store_invoice(connection, invoices[0], "opus")
    return connection


def newest_review(conn: sqlite3.Connection) -> pricing.InvoiceReview:
    invoice_id = conn.execute("SELECT id FROM supplier_invoices WHERE invoice_date = '2026-11-19'").fetchone()[0]
    review = pricing.build_review(conn, invoice_id)
    assert review is not None
    return review


def line_named(review: pricing.InvoiceReview, name: str) -> pricing.PriceLine:
    return next(line for line in review.lines if line.name_latin == name)


@pytest.mark.parametrize(
    ("unit_price", "amount", "per_box", "note", "shop_price", "cost", "rule"),
    [
        (1000, 2000, "", "", None, 1000, "piece"),
        (37000, 37000, "24 EA", "", 1500, 1542, "box"),
        (1500, 3000, "24 EA", "", 2000, 1500, "piece"),
        (12000, 12000, "", "Quantity : 3 pcs", None, 4000, "pack"),
        (34560, 0, "24 EA", "", 1800, 1440, "box"),
        (1440, 0, "24 EA", "", 1800, 1440, "piece"),
        (34560, 0, "24 EA", "", None, 34560, "piece"),
    ],
)
def test_unit_cost_rules(
    unit_price: int, amount: int, per_box: str, note: str, shop_price: int | None, cost: int, rule: str
) -> None:
    result = pricing.unit_cost(unit_price, amount, per_box, note, shop_price)
    assert (result.cost_won, result.rule) == (cost, rule)


def test_unit_cost_rejects_free_rows() -> None:
    with pytest.raises(ValueError):
        pricing.unit_cost(0, 0, "", "", None)


@pytest.mark.parametrize(
    ("cost", "pct", "price"), [(1542, 15, 1800), (1000, 10, 1100), (1001, 0, 1100), (16400, 15, 18900), (100, 50, 200)]
)
def test_suggest_price_rounds_up_to_the_next_hundred(cost: int, pct: int, price: int) -> None:
    assert pricing.suggest_price(cost, pct) == price


def test_suggest_price_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        pricing.suggest_price(0, 15)


def test_needs_raise_flags_under_ten_percent() -> None:
    assert not pricing.needs_raise(1100, 1000)
    assert pricing.needs_raise(1099, 1000)


def test_per_box_count_defaults_to_one() -> None:
    assert pricing.per_box_count("24 EA") == 24
    assert pricing.per_box_count("") == 1
    assert pricing.per_box_count("0 EA") == 1


def test_rank_products_finds_the_closest_name(conn: sqlite3.Connection) -> None:
    shop = pricing.load_shop(conn)
    best, score = pricing.rank_products("Hydro Coco 330ml", shop, 1)[0]
    assert best.id == 1
    assert score >= pricing.MATCH_SCORE_MIN
    assert pricing.rank_products("  ", shop, 3) == []


def test_build_review_sorts_lines_into_fix_ok_and_no_match(conn: sqlite3.Connection) -> None:
    review = newest_review(conn)
    names = [line.name_latin for line in review.lines]
    assert "Shipping Fee" not in names

    hydro = line_named(review, "Hydro Coco 330ml")
    assert (hydro.cost.cost_won, hydro.prev_cost_won, hydro.status) == (1542, 1500, "fix")
    assert hydro.suggested_price == 1800
    assert dict(hydro.suggestions)[50] == 2400

    indomie = line_named(review, "Indomie Mi Goreng")
    assert (indomie.cost.cost_won, indomie.status, indomie.prev_cost_won) == (750, "fix", None)
    assert line_named(review, "Dua Kelinci Sukro 95g").status == "ok"
    assert line_named(review, "Zzyzx Qwerty Snack 100g").status == "no_match"

    assert review.to_fix_count == 3
    assert {line.name_latin for line in review.folded} == {"Dua Kelinci Sukro 95g"}


def test_update_sets_price_and_cost_then_undo_restores_them(conn: sqlite3.Connection) -> None:
    hydro = line_named(newest_review(conn), "Hydro Coco 330ml")
    pricing.apply_update(conn, hydro.item_id, 1, 1800)

    assert conn.execute("SELECT price, purchase_price FROM products WHERE id = 1").fetchone() == (1800, 1542)
    after = line_named(newest_review(conn), "Hydro Coco 330ml")
    assert after.review is not None and after.review.new_price == 1800
    assert after.match_source == "saved"
    assert newest_review(conn).to_fix_count == 2
    with pytest.raises(ReviewError):
        pricing.apply_update(conn, hydro.item_id, 1, 1900)

    pricing.undo_review(conn, hydro.item_id)
    assert conn.execute("SELECT price, purchase_price FROM products WHERE id = 1").fetchone() == (1500, None)
    assert line_named(newest_review(conn), "Hydro Coco 330ml").review is None


def test_undo_leaves_a_price_someone_changed_since(conn: sqlite3.Connection) -> None:
    hydro = line_named(newest_review(conn), "Hydro Coco 330ml")
    pricing.apply_update(conn, hydro.item_id, 1, 1800)
    conn.execute("UPDATE products SET price = 2000 WHERE id = 1")
    conn.commit()
    with pytest.raises(ReviewError, match="changed again"):
        pricing.undo_review(conn, hydro.item_id)
    assert conn.execute("SELECT price FROM products WHERE id = 1").fetchone() == (2000,)


def test_update_rejects_bad_prices_and_missing_products(conn: sqlite3.Connection) -> None:
    hydro = line_named(newest_review(conn), "Hydro Coco 330ml")
    with pytest.raises(ReviewError):
        pricing.apply_update(conn, hydro.item_id, 1, 0)
    with pytest.raises(ReviewError):
        pricing.apply_update(conn, hydro.item_id, 999, 1800)
    assert conn.execute("SELECT price FROM products WHERE id = 1").fetchone() == (1500,)


def test_skip_records_once_and_undo_clears_it(conn: sqlite3.Connection) -> None:
    indomie = line_named(newest_review(conn), "Indomie Mi Goreng")
    pricing.apply_skip(conn, indomie.item_id)
    assert line_named(newest_review(conn), "Indomie Mi Goreng").review is not None
    with pytest.raises(ReviewError):
        pricing.apply_skip(conn, indomie.item_id)
    pricing.undo_review(conn, indomie.item_id)
    with pytest.raises(ReviewError):
        pricing.undo_review(conn, indomie.item_id)


def test_set_match_picks_a_product_or_marks_not_sold(conn: sqlite3.Connection) -> None:
    snack = line_named(newest_review(conn), "Zzyzx Qwerty Snack 100g")
    pricing.set_match(conn, snack.item_id, 3)
    matched = line_named(newest_review(conn), "Zzyzx Qwerty Snack 100g")
    assert matched.shop is not None and matched.shop.id == 3
    assert matched.match_source == "saved"

    pricing.set_match(conn, snack.item_id, None)
    assert line_named(newest_review(conn), "Zzyzx Qwerty Snack 100g").status == "not_sold"
    with pytest.raises(ReviewError):
        pricing.set_match(conn, snack.item_id, 999)


def test_recent_invoices_lists_newest_first(conn: sqlite3.Connection) -> None:
    recent = pricing.recent_invoices(conn, 5)
    assert [r.invoice.invoice_date for r in recent] == ["2026-11-19", "2026-11-05"]
    with pytest.raises(ValueError):
        pricing.recent_invoices(conn, 0)
