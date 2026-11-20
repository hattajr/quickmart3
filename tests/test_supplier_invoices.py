"""Tests for supplier invoice parsing, page grouping and storage."""

import io
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.supplier_invoices.ledger import (  # noqa: E402
    apply_schema,
    build_invoices,
    is_invoice_stored,
    is_photo_stored,
    load_known_products,
    store_invoice,
)
from app.services.supplier_invoices.parse import (  # noqa: E402
    Item,
    Page,
    format_known_products,
    normalize_supplier,
    page_from_answer,
    upright_jpeg,
)

FAKE_IMAGE = (b"\xff\xd8jpeg", 10, 20)


def make_item(name_latin: str = "Product", qty: int = 1, unit_price: int = 1000, struck_out: bool = False) -> Item:
    return Item("상품", name_latin, "24 EA", "1", qty, unit_price, qty * unit_price, "", struck_out)


def make_page(
    name: str,
    page_no: int,
    page_count: int,
    qty: int,
    total: tuple[int, int] | None,
    invoice_no: str = "16",
    invoice_date: str = "2026-09-17",
    items: list[Item] | None = None,
) -> Page:
    """Build a page; by default it holds one item of qty units at 1,000 won."""
    return Page(
        source_file=name,
        source_sha256=f"sha-{name}",
        rotation_deg=0,
        image_jpeg=FAKE_IMAGE[0],
        image_width_px=FAKE_IMAGE[1],
        image_height_px=FAKE_IMAGE[2],
        raw_json="{}",
        supplier="하나피아",
        document_type="거래명세표",
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        page_no=page_no,
        page_count=page_count,
        total_qty=None if total is None else total[0],
        total_amount=None if total is None else total[1],
        items=items if items is not None else [make_item(qty=qty)],
    )


def answer(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "supplier": "(주)하나피아",
        "document_type": "거래명세표",
        "invoice_no": "16",
        "date": "2026-09-17",
        "page_no": 1,
        "page_count": 1,
        "total_qty": 1,
        "total_amount": 1000,
        "items": [
            {
                "product_name": "상품 -",
                "name_latin": "Product",
                "items_per_box": "",
                "box": "",
                "qty": 1,
                "unit_price": 1000,
                "amount": 1000,
                "note": "",
                "struck_out": False,
            }
        ],
    }
    base.update(overrides)
    return base


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON;")
    apply_schema(connection)
    return connection


def test_two_pages_merge_into_one_invoice() -> None:
    pages = [make_page("b.jpg", 2, 2, 5, (15, 15000)), make_page("a.jpg", 1, 2, 10, (15, 15000))]
    invoices, failures = build_invoices(pages)
    assert failures == []
    assert [p.page_no for p in invoices[0].pages] == [1, 2]
    assert invoices[0].total_amount == 15000


def test_duplicate_photo_of_same_page_is_ignored() -> None:
    pages = [make_page("a.jpg", 1, 1, 10, (10, 10000)), make_page("a 2.jpg", 1, 1, 10, (10, 10000))]
    invoices, failures = build_invoices(pages)
    assert failures == []
    assert len(invoices[0].pages) == 1


def test_missing_page_fails_the_totals_check() -> None:
    invoices, failures = build_invoices([make_page("a.jpg", 1, 2, 10, (15, 15000))])
    assert invoices == []
    assert len(failures) == 1
    assert "no photo of page 2 of 2" in failures[0]


def test_newest_photo_wins_between_two_valid_versions() -> None:
    pages = [
        make_page("PHOTO-2026-09-17-09-02-26.jpg", 1, 1, 12, (12, 12000)),
        make_page("PHOTO-2026-09-17-21-27-52.jpg", 1, 2, 10, (15, 15000)),
        make_page("PHOTO-2026-09-17-21-27-52 2.jpg", 2, 2, 5, (15, 15000)),
    ]
    invoices, failures = build_invoices(pages)
    assert failures == []
    assert invoices[0].total_amount == 15000
    assert len(invoices[0].pages) == 2


def test_misread_page_total_is_overruled_by_item_sum() -> None:
    pages = [make_page("a.jpg", 1, 2, 10, (10, 10000)), make_page("b.jpg", 2, 2, 0, (10, 7777))]
    invoices, failures = build_invoices(pages)
    assert failures == []
    assert invoices[0].total_amount == 10000


def test_disagreeing_totals_with_no_match_fail() -> None:
    pages = [make_page("a.jpg", 1, 2, 10, (11, 11000)), make_page("b.jpg", 2, 2, 0, (12, 12000))]
    invoices, failures = build_invoices(pages)
    assert invoices == []
    assert len(failures) == 1


def test_row_arithmetic_mismatch_fails() -> None:
    bad = Item("상품", "Product", "", "", 2, 1000, 1500, "", False)
    invoices, failures = build_invoices([make_page("a.jpg", 1, 1, 0, (2, 1500), items=[bad])])
    assert invoices == []
    assert len(failures) == 1


def test_different_dates_make_separate_invoices() -> None:
    pages = [
        make_page("a.jpg", 1, 1, 10, (10, 10000), invoice_date="2026-09-17"),
        make_page("b.jpg", 1, 1, 10, (10, 10000), invoice_date="2026-10-15"),
    ]
    invoices, failures = build_invoices(pages)
    assert failures == []
    assert [i.invoice_date for i in invoices] == ["2026-09-17", "2026-10-15"]


def test_normalize_supplier_drops_legal_suffix_and_case() -> None:
    assert normalize_supplier("(주)하나피아") == "하나피아"
    assert normalize_supplier(" HalalDojang ") == "halaldojang"


def test_page_from_answer_cleans_fields() -> None:
    page = page_from_answer(answer(), "x.jpg", "sha", 90, FAKE_IMAGE)
    assert page.supplier == "하나피아"
    assert page.items[0].product_name == "상품"
    assert page.rotation_deg == 90


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"page_no": 3, "page_count": 2}, "page marker"),
        ({"date": "2026.09.17"}, "bad date"),
        ({"items": []}, "item count"),
        ({"supplier": "(주)"}, "no supplier"),
    ],
)
def test_page_from_answer_rejects_bad_fields(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        page_from_answer(answer(**overrides), "x.jpg", "sha", 0, FAKE_IMAGE)


def test_upright_jpeg_rotates_clockwise_and_caps_size() -> None:
    wide = Image.new("RGB", (3000, 1000), "white")
    jpeg, width_px, height_px = upright_jpeg(wide, 90)
    assert (width_px, height_px) == (667, 2000)
    assert Image.open(io.BytesIO(jpeg)).size == (667, 2000)
    with pytest.raises(ValueError, match="rotation"):
        upright_jpeg(wide, 45)


def test_format_known_products_lists_names() -> None:
    assert format_known_products({}) == "(none yet)"
    assert format_known_products({("하나피아", "Khong Guan Classic 350g"): "콩구안 클래식 350g"}) == (
        "하나피아 | Khong Guan Classic 350g | 콩구안 클래식 350g"
    )


def test_store_keeps_photo_and_shares_products(conn: sqlite3.Connection) -> None:
    tempe = "Frozen Tempe Ajaki 200g"
    first = build_invoices([make_page("a.jpg", 1, 1, 0, (1, 1500), "1", "2026-10-01", [make_item(tempe, 1, 1500)])])
    second = build_invoices(
        [
            make_page(
                "b.jpg", 1, 1, 0, (3, 11799), "2", "2026-10-08",
                [make_item(tempe, 1, 1800), make_item(tempe, 1, 9999, struck_out=True), make_item(tempe, 1, 0)],
            )
        ]
    )  # fmt: skip
    store_invoice(conn, first[0][0], "opus")
    store_invoice(conn, second[0][0], "opus")

    assert conn.execute("SELECT count(*) FROM supplier_products").fetchone() == (1,)
    assert conn.execute("SELECT image_jpeg, image_width_px FROM supplier_invoice_pages LIMIT 1").fetchone() == (
        FAKE_IMAGE[0],
        FAKE_IMAGE[1],
    )
    assert is_photo_stored(conn, "sha-a.jpg")
    assert is_invoice_stored(conn, "하나피아", "2026-10-08")
    assert not is_invoice_stored(conn, "하나피아", "2026-10-09")
    assert load_known_products(conn) == {("하나피아", tempe): "상품"}
    history = conn.execute(
        "SELECT invoice_date, unit_price, change FROM supplier_price_history ORDER BY invoice_date"
    ).fetchall()
    assert history == [("2026-10-01", 1500, None), ("2026-10-08", 1800, 300)]


def test_same_supplier_and_date_cannot_be_stored_twice(conn: sqlite3.Connection) -> None:
    invoice = build_invoices([make_page("a.jpg", 1, 1, 1, (1, 1000))])[0][0]
    store_invoice(conn, invoice, "opus")
    again = build_invoices([make_page("c.jpg", 1, 1, 1, (1, 1000))])[0][0]
    with pytest.raises(sqlite3.IntegrityError):
        store_invoice(conn, again, "opus")
    assert conn.execute("SELECT count(*) FROM supplier_invoices").fetchone() == (1,)
