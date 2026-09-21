"""Fuzzy matching engine and candidate scoring for product identification."""

import re
from difflib import SequenceMatcher
from typing import Any

from loguru import logger

from app.config import DEBUG

# Generic non-identifying category words
STOP_WORDS = {
    "air",
    "kelapa",
    "rasa",
    "original",
    "bumbu",
    "instan",
    "minuman",
    "makanan",
    "spicy",
    "biskuit",
    "wafer",
    "kue",
    "snack",
    "tepung",
    "goreng",
}

# Known OCR Vision LLM misreadings, double consonant variations & spelling normalization map
OCR_CORRECTIONS = {
    "emiring": "emping",
    "mejang": "melinjo",
    "cuplis": "emping",
    "tehbotol": "teh botol",
    "boncabe": "bon cabe",
    "hydrococo": "hydro coco",
    "bapper": "baper",
}

FORM_SYNONYMS = {
    "botol": {"botol", "bottle", "btl", "jar", "canister", "pot", "toples"},
    "sachet": {"sachet", "sch", "saset", "packet", "sachets", "scht", "pouch", "refill", "bag", "plastik"},
    "kotak": {"kotak", "box", "carton", "tetra", "pack", "dus"},
    "kaleng": {"kaleng", "can", "tin"},
    "cup": {"cup", "bowl", "mangkok"},
}


def normalize_text(text: str) -> str:
    """Normalize string by lowercasing, removing punctuation, and applying OCR corrections."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\bmi\b", "mie", text)

    words = text.split()
    corrected_words = [OCR_CORRECTIONS.get(w, w) for w in words]
    text = " ".join(corrected_words)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def string_similarity(a: str, b: str) -> float:
    """Calculate normalized similarity ratio between two strings (0.0 to 1.0)."""
    norm_a = normalize_text(a)
    norm_b = normalize_text(b)
    if not norm_a or not norm_b:
        return 0.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


def fuzzy_token_match(token: str, cand_norm: str, threshold: float = 0.75) -> bool:
    """Check if token matches candidate text either exactly or via fuzzy string similarity."""
    tok_clean = token.lower()
    tok_no_spaces = tok_clean.replace(" ", "")
    cand_no_spaces = cand_norm.replace(" ", "")

    if tok_clean in cand_norm or (len(tok_no_spaces) >= 4 and tok_no_spaces in cand_no_spaces):
        return True

    for cand_word in cand_norm.split():
        if abs(len(cand_word) - len(tok_clean)) <= 2 and len(tok_clean) >= 4:
            if SequenceMatcher(None, tok_clean, cand_word).ratio() >= threshold:
                return True

    return False


def is_token_subset_fuzzy(target_tokens: set[str], cand_tokens: set[str], cand_norm: str, threshold: float = 0.70) -> bool:
    """Check if a significant subset of target tokens match candidate tokens or fuzzy match."""
    if not target_tokens:
        return False

    matched_count = 0
    for t in target_tokens:
        if t in cand_tokens or t in cand_norm:
            matched_count += 1
        else:
            for cw in cand_tokens:
                if abs(len(t) - len(cw)) <= 1 and len(t) >= 4:
                    if SequenceMatcher(None, t, cw).ratio() >= 0.85:
                        matched_count += 1
                        break

    return (matched_count / len(target_tokens)) >= threshold


def get_form_category(word: str) -> str | None:
    """Map word to standard packaging form category."""
    w = word.lower()
    for cat, synonyms in FORM_SYNONYMS.items():
        if w in synonyms:
            return cat
    return None


def score_form(cand_name: str, llm_form: str) -> float:
    """Soft bonus for matching container form (+20.0)."""
    if not llm_form:
        return 0.0

    target_cat = get_form_category(llm_form) or llm_form.lower()
    cand_words = set(re.findall(r"\b\w+\b", cand_name.lower()))

    cand_cats = set()
    for w in cand_words:
        c = get_form_category(w)
        if c:
            cand_cats.add(c)

    if target_cat in cand_cats:
        return 20.0
    return 0.0


def generate_search_queries(llm_info: dict[str, Any]) -> list[str]:
    """Generate search query fallback sequence from Vision LLM output."""
    queries = []

    sq = llm_info.get("search_query", "").strip()
    if sq:
        queries.append(sq)

    brand = llm_info.get("brand", "").strip()
    product_name = llm_info.get("product_name", "").strip()
    variant = llm_info.get("variant", "").strip()
    size = llm_info.get("size", "").strip()
    form = llm_info.get("form", "").strip()

    if brand and product_name:
        queries.append(f"{brand} {product_name}")

    if product_name and variant:
        queries.append(f"{product_name} {variant}")

    if product_name:
        queries.append(product_name)

    if brand:
        queries.append(brand)

    for w in [variant, form, size]:
        if w and len(w) >= 3 and w.lower() not in STOP_WORDS and w not in queries:
            queries.append(w)

    final_queries = []
    for q in queries:
        clean_q = normalize_text(q)
        if clean_q and clean_q not in [normalize_text(x) for x in final_queries]:
            final_queries.append(q)
        if len(final_queries) >= 5:
            break

    return final_queries


def score_candidate(candidate: dict[str, Any], llm_info: dict[str, Any]) -> float:
    """Score candidate using brand, product identity tokens, fuzzy OCR matching, form bonus, and penalties."""
    cand_raw_name = candidate.get("name") or candidate.get("displayed_name") or ""
    cand_norm = normalize_text(cand_raw_name)
    cand_tokens = set(cand_norm.split())

    brand = normalize_text(llm_info.get("brand", ""))
    product_name = normalize_text(llm_info.get("product_name", ""))
    variant = normalize_text(llm_info.get("variant", ""))
    size = normalize_text(llm_info.get("size", ""))
    form = llm_info.get("form", "").strip()

    combined_identity = normalize_text(f"{brand} {product_name}")
    id_tokens = [w for w in re.findall(r"\b\w+\b", combined_identity) if len(w) > 2 and w.lower() not in STOP_WORDS]

    if id_tokens:
        has_id_match = False
        for token in id_tokens:
            if fuzzy_token_match(token, cand_norm):
                has_id_match = True
                break

        if not has_id_match:
            return 0.0

    target_full = normalize_text(f"{brand} {product_name} {variant} {form} {size}")
    target_tokens = set(target_full.split())

    score = 0.0

    if product_name:
        pname_toks = set(re.findall(r"\b\w+\b", product_name))
        if is_token_subset_fuzzy(pname_toks, cand_tokens, cand_norm):
            score += 40.0
        else:
            score += string_similarity(product_name, cand_norm) * 25.0

    if brand:
        brand_toks = set(re.findall(r"\b\w+\b", brand))
        if is_token_subset_fuzzy(brand_toks, cand_tokens, cand_norm):
            score += 20.0
        else:
            score += string_similarity(brand, cand_norm) * 10.0

    if variant and variant in cand_norm:
        score += 15.0

    score += score_form(cand_raw_name, form)

    if size and size in cand_norm:
        score += 10.0

    filtered_cand_tokens = {t for t in cand_tokens if get_form_category(t) is None and t not in STOP_WORDS}
    filtered_target_tokens = {t for t in target_tokens if get_form_category(t) is None and t not in STOP_WORDS}
    extra_tokens = filtered_cand_tokens - filtered_target_tokens
    if extra_tokens:
        score -= len(extra_tokens) * 2.0

    if target_full:
        sim = string_similarity(target_full, cand_norm)
        score += sim * 15.0

    return score


def select_best_candidate(candidates: list[dict[str, Any]], llm_info: dict[str, Any]) -> dict[str, Any] | None:
    """Select candidate item with highest score."""
    if not candidates:
        return None

    scored_candidates = []
    for cand in candidates:
        score = score_candidate(cand, llm_info)
        if score > 0.0:
            scored_candidates.append((score, cand))

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_cand = scored_candidates[0]

    if DEBUG:
        logger.info("Candidate Scores:")
        for score, cand in scored_candidates:
            logger.info(f"  ID {cand.get('id') or cand.get('product_id')} | '{cand.get('name')}' -> Score: {score:.2f}")

    if best_score < 10.0:
        if DEBUG:
            logger.info(f"Best score {best_score:.2f} is below minimum threshold 10.0")
        return None

    return best_cand


def get_top_candidates(candidates: list[dict[str, Any]], llm_info: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """Rank top candidates when no single candidate has high confidence."""
    if not candidates:
        return []

    seen_ids = set()
    unique_candidates = []
    for cand in candidates:
        pid = cand.get("id") or cand.get("product_id")
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            unique_candidates.append(cand)

    scored = []
    for cand in unique_candidates:
        score = score_candidate(cand, llm_info)
        if score > 0.0:
            scored.append((score, cand))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [cand for _, cand in scored[:limit]]
