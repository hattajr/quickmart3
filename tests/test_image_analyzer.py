"""Unit and integration tests for product image analyzer service."""

import io
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
os.environ.setdefault("ADMIN_PASSWORD", "testpass")

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.services.image_analyzer.matching import (
    fuzzy_token_match,
    normalize_text,
    score_candidate,
    string_similarity,
)
from app.services.image_analyzer.vision import preprocess_image


def test_preprocess_image_resizes_large_images():
    """Ensure images larger than max_size are downscaled to 1024px JPEG."""
    large_img = Image.new("RGB", (2000, 1500), color="blue")
    buf = io.BytesIO()
    large_img.save(buf, format="JPEG")
    raw_bytes = buf.getvalue()

    processed_bytes, mime = preprocess_image(raw_bytes, max_size=1024)
    assert mime == "image/jpeg"

    res_img = Image.open(io.BytesIO(processed_bytes))
    assert max(res_img.size) <= 1024


def test_matching_normalization_and_fuzzy():
    """Test text normalization and OCR corrections."""
    norm1 = normalize_text("TehBotol Sosro 500ml")
    assert "teh botol" in norm1
    assert "sosro" in norm1

    assert string_similarity("Hydro Coco Original", "Hydro Coco Original 250ml") > 0.7
    assert fuzzy_token_match("tehbotol", "teh botol sosro 500ml") is True


def test_score_candidate():
    """Test candidate scoring for exact/fuzzy product name match."""
    llm_info = {
        "brand": "Hydro Coco",
        "product_name": "Original",
        "variant": "Original",
        "size": "250ml",
        "form": "kotak",
    }
    candidate = {"id": 1, "name": "Hydro Coco Original 250ml Kotak"}

    score = score_candidate(candidate, llm_info)
    assert score >= 45.0


@pytest.mark.asyncio
async def test_api_identify_disabled_or_endpoint(monkeypatch):
    """Test analyze_product_image with mocked Vision LLM response."""
    from app.services.image_analyzer import service

    async def mock_identify(*args, **kwargs):
        return {
            "brand": "Teh Botol",
            "product_name": "Sosro",
            "variant": "",
            "size": "",
            "form": "botol",
            "search_query": "Teh Botol Sosro",
        }

    monkeypatch.setattr("app.services.image_analyzer.service.identify_product_image", mock_identify)

    img = Image.new("RGB", (100, 100), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    test_bytes = buf.getvalue()

    res = await service.analyze_product_image(test_bytes, "image/jpeg")
    assert "status" in res


def test_identify_rejects_empty_image_as_client_error():
    """An empty image upload remains a 400 response rather than becoming a 500."""
    response = TestClient(app).post(
        "/api/identify",
        files={"file": ("empty.jpg", b"", "image/jpeg")},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Empty image uploaded."}
