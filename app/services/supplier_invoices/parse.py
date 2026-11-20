"""Turn one invoice photo into a parsed page with headless Claude Code (`claude -p`)."""

import io
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

CLAUDE_TIMEOUT_SECONDS = 600
ITEMS_PER_PAGE_MAX = 200
KNOWN_PRODUCTS_MAX = 1000
ORIENTATION_IMAGE_SIDE_PX = 1024
STORED_IMAGE_SIDE_PX_MAX = 2000
STORED_IMAGE_BYTES_MAX = 5 * 1024 * 1024
PAGE_MODEL_DEFAULT = "opus"
ORIENTATION_MODEL = "sonnet"
# Bump when PAGE_PROMPT or PAGE_SCHEMA changes so cached answers are not reused.
CACHE_VERSION = 2

ORIENTATION_PROMPT = """Look at the photo at {path} with the Read tool.
How many degrees must the photo be rotated CLOCKWISE so that the printed text reads upright,
left to right? Answer 0, 90, 180 or 270."""

ORIENTATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"rotate_clockwise": {"type": "integer", "enum": [0, 90, 180, 270]}},
    "required": ["rotate_clockwise"],
}

PAGE_PROMPT = """The photo at {path} is ONE page of a supplier document for a shop that sells products
mostly imported from Indonesia. It is either a Korean invoice (거래명세표) or a web order sheet
(주문상세정보). Open it with the Read tool and extract this page.

Fields:
- supplier: the seller's name as printed, without legal suffixes such as (주) or 주식회사.
  On a 거래명세표 it is the 공급자 상호 (e.g. "하나피아"), not the receiver. If no seller name is
  printed, use the name in the website address (e.g. "halaldojang").
- document_type: "거래명세표" or "주문상세정보" (or the printed title if it is something else).
- invoice_no: on a 거래명세표, the number before the page marker (e.g. "16" from "16-1/2");
  on an order sheet, the 주문번호.
- date: YYYY-MM-DD. The 거래명세표 date, or the order sheet's 주문일자.
- page_no, page_count: from a 거래명세표 page marker like "16-1/2" -> 1 and 2. For an order
  sheet, 1 and 1 unless the item table clearly continues on another page.
- total_qty, total_amount: the printed grand totals (총수량 and 합계금액 / 소계 on a 거래명세표;
  the 계 row's 수량 and 상품구매금액 on an order sheet). null if this page shows none.
- items: one per product row on this page, in printed order. Skip "이하여백" and footer lines.
  - product_name: the product text exactly as printed, without the trailing "-" spec separator.
  - name_latin: the product's original name in Latin script as sold in Indonesia
    (brand + product + variant + size), e.g. "콩구안 클래식 350g" -> "Khong Guan Classic 350g".
    This name identifies the product across invoices, so if the row is a product in the
    known products list below, copy that name_latin EXACTLY, even if you would spell it
    differently. Otherwise use the real brand spelling when you recognise it, or romanize.
    If the printed name is already Latin, use it without codes in brackets.
  - items_per_box: per-box count, e.g. "24EA/1BOX" -> "24 EA"; "" if not shown.
  - box: the box column as printed, but "N/0" becomes "N" ("1/0" -> "1", "0/24" stays); "" if none.
  - qty, unit_price, amount: integers in won, no commas. Use printed values, not handwriting.
  - note: the 적요 / remark text for the row, or "".
  - struck_out: true if the row is crossed out by hand with a pen line, otherwise false.

Known products (supplier | name_latin | product_name as last printed):
{known_products}
"""

_ITEM_TEXT_FIELDS = ("product_name", "name_latin", "items_per_box", "box", "note")
_ITEM_INT_FIELDS = ("qty", "unit_price", "amount")
PAGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "supplier": {"type": "string"},
        "document_type": {"type": "string"},
        "invoice_no": {"type": "string"},
        "date": {"type": "string"},
        "page_no": {"type": "integer"},
        "page_count": {"type": "integer"},
        "total_qty": {"type": ["integer", "null"]},
        "total_amount": {"type": ["integer", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **{name: {"type": "string"} for name in _ITEM_TEXT_FIELDS},
                    **{name: {"type": "integer"} for name in _ITEM_INT_FIELDS},
                    "struck_out": {"type": "boolean"},
                },
                "required": [*_ITEM_TEXT_FIELDS, *_ITEM_INT_FIELDS, "struck_out"],
            },
        },
    },
    "required": [
        "supplier", "document_type", "invoice_no", "date",
        "page_no", "page_count", "total_qty", "total_amount", "items",
    ],
}  # fmt: skip

KnownProducts = dict[tuple[str, str], str]
"""Maps (supplier, name_latin) to the product name as last printed."""


@dataclass
class Item:
    product_name: str
    name_latin: str
    items_per_box: str
    box: str
    qty: int
    unit_price: int
    amount: int
    note: str
    struck_out: bool


@dataclass
class Page:
    source_file: str
    source_sha256: str
    rotation_deg: int
    image_jpeg: bytes
    image_width_px: int
    image_height_px: int
    raw_json: str
    supplier: str
    document_type: str
    invoice_no: str
    invoice_date: str
    page_no: int
    page_count: int
    total_qty: int | None
    total_amount: int | None
    items: list[Item]


def run_claude(message: str, schema: dict[str, object], model: str, work_dir: Path) -> dict[str, object]:
    """Run headless Claude Code with only the Read tool and return its structured output."""
    command = [
        "claude", "-p", message,
        "--model", model,
        "--system-prompt", "You extract structured data from document photos.",
        "--tools", "Read", "--allowedTools", "Read",
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
    ]  # fmt: skip
    result = subprocess.run(
        command, cwd=work_dir, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude exited {result.returncode}: {result.stderr or result.stdout}")
    envelope = json.loads(result.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude reported an error: {envelope.get('result')}")
    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        raise RuntimeError(f"claude returned no structured output: {str(envelope.get('result'))[:200]}")
    return structured


def detect_rotation_deg(image: Image.Image, work_dir: Path) -> int:
    """Ask a fast model how many degrees clockwise make the photo upright."""
    small = image.copy()
    small.thumbnail((ORIENTATION_IMAGE_SIDE_PX, ORIENTATION_IMAGE_SIDE_PX))
    small_path = work_dir / "orientation.jpg"
    small.convert("RGB").save(small_path, quality=85)
    answer = run_claude(ORIENTATION_PROMPT.format(path=small_path), ORIENTATION_SCHEMA, ORIENTATION_MODEL, work_dir)
    rotation_deg = answer["rotate_clockwise"]
    if rotation_deg not in (0, 90, 180, 270):
        raise ValueError(f"orientation model answered {rotation_deg!r}")
    return int(str(rotation_deg))


def upright_jpeg(image: Image.Image, rotation_deg: int) -> tuple[bytes, int, int]:
    """Rotate the photo upright, cap its size, and return JPEG bytes with width and height."""
    if rotation_deg not in (0, 90, 180, 270):
        raise ValueError(f"rotation must be 0, 90, 180 or 270, got {rotation_deg}")
    # PIL rotates counterclockwise for positive angles.
    upright = image.rotate(-rotation_deg, expand=True) if rotation_deg else image.copy()
    upright.thumbnail((STORED_IMAGE_SIDE_PX_MAX, STORED_IMAGE_SIDE_PX_MAX))
    buffer = io.BytesIO()
    upright.convert("RGB").save(buffer, format="JPEG", quality=85)
    jpeg = buffer.getvalue()
    if len(jpeg) > STORED_IMAGE_BYTES_MAX:
        raise ValueError(f"upright photo is {len(jpeg)} bytes, over {STORED_IMAGE_BYTES_MAX}")
    return jpeg, upright.width, upright.height


def normalize_supplier(name: str) -> str:
    """Collapse legal suffixes, spaces and case so one supplier always gets one key."""
    return re.sub(r"\(주\)|주식회사|\s+", "", name).lower()


def format_known_products(known: KnownProducts) -> str:
    """Render known products for the prompt, newest last, within the prompt budget."""
    if not known:
        return "(none yet)"
    rows = list(known.items())[-KNOWN_PRODUCTS_MAX:]
    return "\n".join(f"{supplier} | {name_latin} | {product_name}" for (supplier, name_latin), product_name in rows)


def _items_from_answer(items_raw: object, source_file: str) -> list[Item]:
    """Build items from the answer's item list, rejecting an empty or oversized page."""
    if not isinstance(items_raw, list):
        raise ValueError(f"{source_file}: items is not a list")
    if not 0 < len(items_raw) <= ITEMS_PER_PAGE_MAX:
        raise ValueError(f"{source_file}: item count {len(items_raw)} out of range")
    return [
        Item(
            product_name=str(row["product_name"]).strip().rstrip("-").strip(),
            name_latin=str(row["name_latin"]).strip(),
            items_per_box=str(row["items_per_box"]).strip(),
            box=str(row["box"]).strip(),
            qty=int(row["qty"]),
            unit_price=int(row["unit_price"]),
            amount=int(row["amount"]),
            note=str(row["note"]).strip(),
            struck_out=bool(row["struck_out"]),
        )
        for row in items_raw
    ]


def page_from_answer(
    answer: dict[str, object],
    source_file: str,
    source_sha256: str,
    rotation_deg: int,
    image: tuple[bytes, int, int],
) -> Page:
    """Build a Page from the model's structured answer, rejecting malformed fields."""
    items = _items_from_answer(answer["items"], source_file)
    if any(not item.name_latin for item in items):
        raise ValueError(f"{source_file}: an item has no name_latin")
    invoice_date = str(answer["date"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", invoice_date):
        raise ValueError(f"{source_file}: bad date {invoice_date!r}")
    page_no, page_count = int(str(answer["page_no"])), int(str(answer["page_count"]))
    if not 1 <= page_no <= page_count:
        raise ValueError(f"{source_file}: bad page marker {page_no}/{page_count}")
    supplier = normalize_supplier(str(answer["supplier"]))
    if not supplier:
        raise ValueError(f"{source_file}: no supplier")
    total_qty, total_amount = answer["total_qty"], answer["total_amount"]
    image_jpeg, width_px, height_px = image
    return Page(
        source_file=source_file,
        source_sha256=source_sha256,
        rotation_deg=rotation_deg,
        image_jpeg=image_jpeg,
        image_width_px=width_px,
        image_height_px=height_px,
        raw_json=json.dumps(answer, ensure_ascii=False),
        supplier=supplier,
        document_type=str(answer["document_type"]).strip(),
        invoice_no=str(answer["invoice_no"]).strip(),
        invoice_date=invoice_date,
        page_no=page_no,
        page_count=page_count,
        total_qty=None if total_qty is None else int(str(total_qty)),
        total_amount=None if total_amount is None else int(str(total_amount)),
        items=items,
    )


def parse_photo(
    source: Path,
    source_sha256: str,
    rotate: str,
    model: str,
    cache_dir: Path,
    known: KnownProducts,
) -> Page:
    """Parse one photo into a Page, reusing a cached answer when the photo was parsed before."""
    if rotate not in ("auto", "0", "90", "180", "270"):
        raise ValueError(f"rotate must be auto, 0, 90, 180 or 270, got {rotate!r}")
    cache_path = cache_dir / f"{source_sha256}.v{CACHE_VERSION}.json"
    image = ImageOps.exif_transpose(Image.open(source))
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        rotation_deg = int(cached["rotation"])
        logger.info("%s: using cached parse", source.name)
        return page_from_answer(
            cached["page"], source.name, source_sha256, rotation_deg, upright_jpeg(image, rotation_deg)
        )

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        rotation_deg = detect_rotation_deg(image, work_dir) if rotate == "auto" else int(rotate)
        upright = upright_jpeg(image, rotation_deg)
        upright_path = work_dir / "upright.jpg"
        upright_path.write_bytes(upright[0])
        prompt = PAGE_PROMPT.format(path=upright_path, known_products=format_known_products(known))
        answer = run_claude(prompt, PAGE_SCHEMA, model, work_dir)
    cache_path.write_text(
        json.dumps({"rotation": rotation_deg, "model": model, "page": answer}, ensure_ascii=False, indent=1)
    )
    page = page_from_answer(answer, source.name, source_sha256, rotation_deg, upright)
    logger.info(
        "%s: rotated %d, %s %s page %d/%d, %d items",
        source.name, rotation_deg, page.supplier, page.invoice_date, page.page_no, page.page_count, len(page.items),
    )  # fmt: skip
    return page
