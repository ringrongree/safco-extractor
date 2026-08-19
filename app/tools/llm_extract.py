"""LLM-backed tools: classify (ambiguous pages only), extraction fallback
(irregular layouts), selector repair. BUILD_SPEC.md §5, §9, §13.

These are the only call sites allowed to reach app.llm. Every call is tagged
with a purpose for the call counter.
"""
from __future__ import annotations

import asyncio
import json
import re
from functools import partial

from app.llm import MODEL_DEFAULT, MODEL_REASONING, llm
from app.schemas import Availability, PageType, Product, PriceStatus


async def _llm_async(messages: list[dict], purpose: str, model: str, **kw) -> str:
    """The OpenAI SDK client is sync; run it off the event loop thread."""
    return await asyncio.to_thread(partial(llm, messages, purpose, model=model, **kw))

_MAX_HTML_CHARS = 12000  # keep prompts small; DeepSeek call cost discipline


def _truncate(html: str) -> str:
    # Strip script/style to save tokens before truncating.
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:_MAX_HTML_CHARS]


async def classify_with_llm(html: str, url: str) -> tuple[PageType, str, float]:
    prompt = (
        "Classify this ecommerce page as exactly one of: category, listing, product, unknown.\n"
        "- category: a page mainly listing sub-category links, little/no product grid.\n"
        "- listing: a page showing a grid of multiple products to browse.\n"
        "- product: a single product's detail page (may have variants).\n"
        f"URL: {url}\n"
        f"HTML (truncated, tags stripped of scripts/styles):\n{_truncate(html)}\n\n"
        'Respond with strict JSON only: {"page_type": "...", "reasoning": "...", "confidence": 0.0-1.0}'
    )
    raw = await _llm_async(
        [{"role": "user", "content": prompt}],
        purpose="classify",
        model=MODEL_DEFAULT,
    )
    try:
        data = json.loads(_extract_json(raw))
        page_type = PageType(data.get("page_type", "unknown"))
        return page_type, data.get("reasoning", ""), float(data.get("confidence", 0.5))
    except Exception:
        return PageType.UNKNOWN, f"LLM classify response unparseable: {raw[:200]}", 0.0


async def extract_fallback_with_llm(html: str, url: str, category_path: list[str]) -> list[Product]:
    """Only reached when structured data AND config selectors both fail."""
    prompt = (
        "Extract product data from this ecommerce product page HTML. "
        "If there are multiple variants (size/material/etc), return one entry per variant.\n"
        f"URL: {url}\n"
        f"HTML (truncated, tags stripped of scripts/styles):\n{_truncate(html)}\n\n"
        "Respond with strict JSON only, a list of objects, each with keys: "
        "name, brand, sku, price (number or null), currency, pack_size, "
        "availability (one of: In stock, Partially in stock, Backordered, Special order, "
        "Out of stock, Unknown), description. Use null for anything not present. "
        "Never invent values."
    )
    raw = await _llm_async(
        [{"role": "user", "content": prompt}],
        purpose="extract_fallback",
        model=MODEL_DEFAULT,
    )
    try:
        items = json.loads(_extract_json(raw))
        if not isinstance(items, list):
            items = [items]
    except Exception:
        return []

    products = []
    for item in items:
        try:
            availability = Availability(item.get("availability")) if item.get("availability") else Availability.UNKNOWN
        except ValueError:
            availability = Availability.UNKNOWN
        price = item.get("price")
        price_status = PriceStatus.VISIBLE if price else PriceStatus.NOT_FOUND
        products.append(
            Product(
                variant_id=item.get("sku"),
                name=item.get("name"),
                brand=item.get("brand"),
                sku=item.get("sku"),
                category_path=category_path,
                url=url,
                crawl_url=url,
                price=float(price) if price else None,
                currency=item.get("currency"),
                price_status=price_status,
                pack_size=item.get("pack_size"),
                availability=availability,
                description=item.get("description"),
                extraction_method="llm",
            )
        )
    return products


async def repair_selector_with_llm(html: str, field_name: str, broken_selector: str) -> str | None:
    """Uses the reasoning-tier model per BUILD_SPEC.md §5."""
    prompt = (
        f"A CSS selector for field '{field_name}' returned no match on this page.\n"
        f"Broken selector: {broken_selector}\n"
        f"HTML (truncated, tags stripped of scripts/styles):\n{_truncate(html)}\n\n"
        "Suggest one replacement CSS selector that would select the correct element(s) "
        f"for '{field_name}'. Respond with strict JSON only: "
        '{"selector": "..." or null}'
    )
    raw = await _llm_async(
        [{"role": "user", "content": prompt}],
        purpose="selector_repair",
        model=MODEL_REASONING,
    )
    try:
        data = json.loads(_extract_json(raw))
        return data.get("selector") or None
    except Exception:
        return None


def _extract_json(raw: str) -> str:
    """DeepSeek sometimes wraps JSON in markdown fences; strip them."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw
