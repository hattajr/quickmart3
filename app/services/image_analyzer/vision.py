"""Vision LLM client and image preprocessing for product recognition."""

import base64
import io
import json
import re
from typing import Any

import httpx
from loguru import logger
from PIL import Image

from app.config import DEBUG, LLM_ENDPOINT, LLM_MODEL

SYSTEM_PROMPT = """You are a product image recognition assistant.

Analyze the provided product image and identify the product as accurately as possible.

Return ONLY JSON:

{
"brand": "...",
"product_name": "...",
"variant": "...",
"size": "...",
"form": "...",
"search_query": "..."
}

Rules:

* form should be the physical packaging container type if identifiable (e.g. "botol", "sachet", "kotak", "kaleng", "cup", "toples"). Flexible pouches or refill bags should be specified as "sachet". Use an empty string if not applicable.
* search_query should be short and suitable for searching an Indonesian grocery database.
* Prefer visible brand/product text over guessing.
* Do not provide pricing.
* Do not provide explanations.
* If variant, size, or form cannot be determined, use an empty string."""


def preprocess_image(image_bytes: bytes, max_size: int = 1024) -> tuple[bytes, str]:
    """Resize image to max_size (1024px) and convert to clean JPEG.

    Prevents VRAM overflow on high-res uploads.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                background.paste(img, mask=img.split()[3])
            else:
                background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        output_buffer = io.BytesIO()
        img.save(output_buffer, format="JPEG", quality=85)
        return output_buffer.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning(f"Image preprocessing warning: {e}. Using raw image bytes.")
        return image_bytes, "image/jpeg"


def clean_json_text(raw_text: str) -> str:
    """Extract JSON content from markdown code blocks or raw text."""
    raw_text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    start_idx = raw_text.find("{")
    end_idx = raw_text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return raw_text[start_idx : end_idx + 1].strip()

    return raw_text


def parse_llm_result(text_content: str) -> dict[str, Any]:
    """Parse JSON output from the Vision LLM, validating key fields."""
    cleaned = clean_json_text(text_content)
    parsed = json.loads(cleaned)

    brand = str(parsed.get("brand") or "").strip()
    product_name = str(parsed.get("product_name") or "").strip()
    variant = str(parsed.get("variant") or "").strip()
    size = str(parsed.get("size") or "").strip()
    form = str(parsed.get("form") or "").strip()
    search_query = str(parsed.get("search_query") or "").strip()

    if not search_query:
        parts = [p for p in [brand, product_name, variant, form, size] if p]
        search_query = " ".join(parts)

    return {
        "brand": brand,
        "product_name": product_name,
        "variant": variant,
        "size": size,
        "form": form,
        "search_query": search_query,
    }


def extract_content_from_response(data: dict[str, Any]) -> str:
    """Extract message string from OpenAI / LM Studio response formats."""
    output = data.get("output")

    if isinstance(output, list):
        message_contents = []
        for block in output:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type in ("message", "text") and "content" in block:
                    message_contents.append(str(block["content"]))
        if message_contents:
            return "\n".join(message_contents)

        non_reasoning = []
        for block in output:
            if isinstance(block, dict) and block.get("type") != "reasoning" and "content" in block:
                non_reasoning.append(str(block["content"]))
        if non_reasoning:
            return "\n".join(non_reasoning)

    elif isinstance(output, str):
        return output
    elif isinstance(output, dict):
        return output.get("content") or output.get("text") or str(output)

    choices = data.get("choices")
    if isinstance(choices, list) and len(choices) > 0:
        choice = choices[0]
        if isinstance(choice, dict):
            if "message" in choice and isinstance(choice["message"], dict):
                return choice["message"].get("content", "")
            elif "text" in choice:
                return choice["text"]

    return str(data)


async def identify_product_image(raw_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """Send image to local Vision LLM to extract product metadata."""
    jpeg_bytes, clean_mime = preprocess_image(raw_bytes)
    b64_image = base64.b64encode(jpeg_bytes).decode("utf-8")
    data_url = f"data:{clean_mime};base64,{b64_image}"

    openai_payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyze product image and identify product."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.1,
    }

    if DEBUG:
        logger.info(f"Sending image ({len(jpeg_bytes)} bytes) to LLM Endpoint: {LLM_ENDPOINT}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            res = await client.post(
                LLM_ENDPOINT,
                json=openai_payload,
                headers={"Content-Type": "application/json"},
            )

            if res.status_code != 200:
                logger.warning(f"Endpoint {LLM_ENDPOINT} returned status {res.status_code}. Attempting legacy fallback...")
                legacy_payload = {
                    "model": LLM_MODEL,
                    "system_prompt": SYSTEM_PROMPT,
                    "input": data_url,
                }
                res = await client.post(
                    "http://localhost:1234/api/v1/chat",
                    json=legacy_payload,
                    headers={"Content-Type": "application/json"},
                )

            res.raise_for_status()
            data = res.json()

            if DEBUG:
                logger.info(f"LLM Raw Response Received: {data}")

            content_text = extract_content_from_response(data)
            result = parse_llm_result(content_text)

            if DEBUG:
                logger.info(f"Parsed Product Info from Vision LLM: {result}")

            return result

        except Exception as e:
            logger.error(f"Error communicating with Vision LLM: {e}")
            raise RuntimeError(f"Vision LLM service error: {str(e)}")
