"""Deterministic JSON-LD oracle vs LLM extract — pure compare, no I/O.

Grades RAW structured_extract_product fields only: name, sku, price, currency,
availability, description, images. specifications and alternatives are ungraded.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.schemas import Availability, Product

# Tunable match thresholds (the deferred "verify threshold", now concretized).
SKU_COLLAPSE_WS = True
PRICE_ABS_TOL = 0.001
AVAILABILITY_ENUM_EXACT = True  # after mapping both sides onto Availability


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_sku(value: Optional[str]) -> str:
    text = _collapse_ws(value or "") if SKU_COLLAPSE_WS else (value or "").strip()
    return text.upper()


def normalize_name(value: Optional[str]) -> str:
    return _collapse_ws(value or "").casefold()


def _availability_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Availability):
        return value.value
    return str(value)


def _price_match(a: Optional[float], b: Optional[float]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= PRICE_ABS_TOL
    except (TypeError, ValueError):
        return False


def _presence(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _image_set(urls: Optional[list[str]]) -> set[str]:
    return {u.strip() for u in (urls or []) if u and str(u).strip()}


def image_overlap_ratio(llm_urls: Optional[list[str]], oracle_urls: Optional[list[str]]) -> float:
    a, b = _image_set(llm_urls), _image_set(oracle_urls)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _field_matches(llm: Product, oracle: Product) -> dict[str, bool]:
    return {
        "sku": normalize_sku(llm.sku) == normalize_sku(oracle.sku) and bool(normalize_sku(llm.sku)),
        "name": normalize_name(llm.name) == normalize_name(oracle.name) and bool(normalize_name(llm.name)),
        "availability": (
            _availability_value(llm.availability) == _availability_value(oracle.availability)
            if AVAILABILITY_ENUM_EXACT
            else False
        ),
        "price": _price_match(llm.price, oracle.price),
        "currency": (llm.currency or "") == (oracle.currency or ""),
        "description": _presence(llm.description) == _presence(oracle.description),
    }


def compare(llm_rows: Optional[list[Product]], oracle_rows: Optional[list[Product]]) -> dict[str, Any]:
    """Grade LLM rows against raw JSON-LD rows. No I/O.

    If the oracle is absent (no JSON-LD — the general-site case), skip grading.
    """
    if oracle_rows is None:
        return {"oracle": "absent"}

    llm_rows = llm_rows or []
    oracle_by_sku: dict[str, Product] = {}
    oracle_only_unkeyed = 0
    for row in oracle_rows:
        key = normalize_sku(row.sku)
        if not key:
            oracle_only_unkeyed += 1
            continue
        oracle_by_sku[key] = row

    llm_by_sku: dict[str, Product] = {}
    llm_only_unkeyed = 0
    for row in llm_rows:
        key = normalize_sku(row.sku)
        if not key:
            llm_only_unkeyed += 1
            continue
        llm_by_sku[key] = row

    matched_skus = sorted(set(llm_by_sku) & set(oracle_by_sku))
    llm_only = sorted(set(llm_by_sku) - set(oracle_by_sku))
    oracle_only = sorted(set(oracle_by_sku) - set(llm_by_sku))

    field_match: dict[str, dict[str, bool]] = {}
    image_ratios: list[float] = []
    for sku in matched_skus:
        llm, oracle = llm_by_sku[sku], oracle_by_sku[sku]
        field_match[sku] = _field_matches(llm, oracle)
        image_ratios.append(image_overlap_ratio(llm.image_urls, oracle.image_urls))

    unmatched = len(llm_only) + len(oracle_only) + llm_only_unkeyed + oracle_only_unkeyed
    row_count_match = (
        len(llm_by_sku) == len(oracle_by_sku)
        and unmatched == 0
        and llm_only_unkeyed == 0
        and oracle_only_unkeyed == 0
    )

    mean_overlap = (sum(image_ratios) / len(image_ratios)) if image_ratios else None

    return {
        "oracle": "present",
        "oracle_present": True,
        "row_count_match": row_count_match,
        "llm_row_count": len(llm_rows),
        "oracle_row_count": len(oracle_rows),
        "matched_skus": matched_skus,
        "llm_only_skus": llm_only,
        "oracle_only_skus": oracle_only,
        "field_match": field_match,
        "image_overlap": mean_overlap,
        "image_overlap_by_sku": dict(zip(matched_skus, image_ratios)),
        "fan_out_diverged": not row_count_match,
    }
