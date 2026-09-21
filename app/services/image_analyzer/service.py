"""Orchestrator service connecting Vision LLM identification with QuickMart internal database search."""

import sqlite3
from typing import Any

from loguru import logger

from app.config import DEBUG, IMAGE_ANALYZER_ENABLED
from app.db.database import get_db
from app.services.image_analyzer.matching import (
    generate_search_queries,
    get_top_candidates,
    score_candidate,
    select_best_candidate,
)
from app.services.image_analyzer.vision import identify_product_image


def _search_internal_products(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search internal SQLite products table for a query string."""
    query_pattern = f"%{query.strip()}%"
    candidates: list[dict[str, Any]] = []

    try:
        for conn in get_db():
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            rows = cursor.execute(
                """
                SELECT id, name, price, image_url, barcode, stock, description
                FROM products
                WHERE name LIKE ? OR keyword LIKE ? OR barcode LIKE ? OR description LIKE ?
                LIMIT ?
                """,
                (query_pattern, query_pattern, query_pattern, query_pattern, limit),
            ).fetchall()

            for row in rows:
                candidates.append({
                    "product_id": row["id"],
                    "id": row["id"],
                    "name": row["name"],
                    "price": float(row["price"] or 0),
                    "image_url": row["image_url"] or "",
                    "barcode": row["barcode"] or "",
                    "displayed_name": row["name"],
                })
    except Exception as e:
        logger.error(f"Error querying products database for '{query}': {e}")

    return candidates


def _get_all_products_snapshot(limit: int = 500) -> list[dict[str, Any]]:
    """Fetch snapshot of all products for broad fuzzy fallback matching."""
    candidates: list[dict[str, Any]] = []
    try:
        for conn in get_db():
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT id, name, price, image_url, barcode FROM products LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                candidates.append({
                    "product_id": row["id"],
                    "id": row["id"],
                    "name": row["name"],
                    "price": float(row["price"] or 0),
                    "image_url": row["image_url"] or "",
                    "barcode": row["barcode"] or "",
                    "displayed_name": row["name"],
                })
    except Exception as e:
        logger.error(f"Error fetching all products snapshot: {e}")
    return candidates


async def analyze_product_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """Analyze uploaded product image and resolve matching product from QuickMart database."""
    if not IMAGE_ANALYZER_ENABLED:
        return {"status": "disabled", "error": "Image analyzer feature is disabled in settings."}

    if not image_bytes:
        return {"status": "error", "error": "Empty image content provided."}

    # Step 1: Send Image to Vision LLM
    logger.info("=== STEP 1: VISION LLM IDENTIFICATION ===")
    llm_info = await identify_product_image(image_bytes, mime_type=mime_type)

    # Step 2: Generate Search Queries with Fallbacks
    search_queries = generate_search_queries(llm_info)
    logger.info(f"=== STEP 2 & 3: INTERNAL PRODUCT MATCHING === Queries: {search_queries}")

    attempted_queries = []
    all_candidates_found = []
    seen_ids: set[int] = set()

    # Query internal products table with each search query
    for query in search_queries:
        attempted_queries.append(query)
        candidates = _search_internal_products(query)
        for c in candidates:
            pid = c["id"]
            if pid not in seen_ids:
                seen_ids.add(pid)
                all_candidates_found.append(c)

        best_cand = select_best_candidate(candidates, llm_info)
        if best_cand:
            cand_score = score_candidate(best_cand, llm_info)
            if cand_score >= 45.0:
                break

    # If specific queries yielded no candidates, score against all products
    if not all_candidates_found:
        logger.info("No candidates returned from specific queries. Running fuzzy match over all products snapshot...")
        all_prods = _get_all_products_snapshot()
        for c in all_prods:
            pid = c["id"]
            if pid not in seen_ids:
                seen_ids.add(pid)
                all_candidates_found.append(c)

    if not all_candidates_found:
        payload = {"status": "not_found"}
        if DEBUG:
            payload["debug"] = {"llm_info": llm_info, "attempted_queries": attempted_queries}
        return payload

    top_cand = select_best_candidate(all_candidates_found, llm_info)
    top_score = score_candidate(top_cand, llm_info) if top_cand else 0.0

    # High Confidence Match (score >= 45.0)
    if top_cand and top_score >= 45.0:
        result_payload = {
            "status": "matched",
            "product_id": top_cand["id"],
            "id": top_cand["id"],
            "name": top_cand["name"],
            "price": top_cand["price"],
            "image_url": top_cand["image_url"],
            "display_name": top_cand["name"],
        }
        if DEBUG:
            result_payload["debug"] = {
                "llm_info": llm_info,
                "score": top_score,
                "attempted_queries": attempted_queries,
            }
        return result_payload

    # Moderate / Ambiguous match: Return "Possible Options"
    top_candidates = get_top_candidates(all_candidates_found, llm_info, limit=3)
    options_list = []

    for cand in top_candidates:
        options_list.append({
            "product_id": cand["id"],
            "id": cand["id"],
            "name": cand["name"],
            "price": cand["price"],
            "image_url": cand["image_url"],
            "display_name": f"{cand['name']} — Rp {cand['price']:,.0f}",
        })

    if options_list:
        payload = {
            "status": "possible_options",
            "message": "This product is possibly:",
            "options": options_list,
        }
        if DEBUG:
            payload["debug"] = {"llm_info": llm_info, "attempted_queries": attempted_queries}
        return payload

    payload = {"status": "not_found"}
    if DEBUG:
        payload["debug"] = {"llm_info": llm_info, "attempted_queries": attempted_queries}
    return payload
