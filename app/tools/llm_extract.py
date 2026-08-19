"""LLM-backed tools: classify, product extract, specs, selector repair.

Option 1 (BUILD_REPORT.md §15): graph unchanged — Safco stays JSON-LD-primary;
Net32 already uses LLM classify + specs. `extract_fallback_with_llm` is a
unified extractor (full field set, one object per variant, 25k window) used
as cascade last-resort, not the happy path. Alias: `extract_with_llm`.
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

_MAX_HTML_CHARS = 12000  # classify + selector repair
# Unified extract uses the 25k window (Net32 spec tables sit past 12k —
# BUILD_REPORT.md §10). Safco variant *rows* do not live in this visible-DOM
# slice; they live in stripped <script> JSON-LD / window.masterData (Phase 0).
_MAX_HTML_CHARS_EXTRACT = 25000
_MAX_HTML_CHARS_SPECS = _MAX_HTML_CHARS_EXTRACT


def _truncate(html: str, max_chars: int = _MAX_HTML_CHARS) -> str:
    # Strip script/style to save tokens before truncating.
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_chars]


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
    try:
        raw = await _llm_async(
            [{"role": "user", "content": prompt}],
            purpose="classify",
            model=MODEL_DEFAULT,
        )
    except Exception as exc:
        # Same degrade as the other three LLM sites: a raised API error must
        # not propagate to classify_node / crash the run. Choke-point logging
        # in llm() already recorded the failure.
        return PageType.UNKNOWN, f"{type(exc).__name__}: {exc}", 0.0
    try:
        data = json.loads(_extract_json(raw))
        page_type = PageType(data.get("page_type", "unknown"))
        return page_type, data.get("reasoning", ""), float(data.get("confidence", 0.5))
    except Exception:
        return PageType.UNKNOWN, f"LLM classify response unparseable: {raw[:200]}", 0.0


async def extract_fallback_with_llm(html: str, url: str, category_path: list[str]) -> list[Product]:
    """Primary product extract: one strict-JSON call, one object per variant.

    Name is historical (it used to be the cascade's last resort). Callers may
    use the extract_with_llm alias. Degrades to [] on API error / unparseable
    JSON — same exception discipline as before, so extract_node can route to
    recover without crashing the run.
    """
    prompt = (
        "Extract product data from this ecommerce product page HTML. "
        "Enumerate EVERY variant (size, material, pack, color, etc.) as its own "
        "object — do not collapse variants into one row. If the page shows a "
        "variant table or list of SKUs, return one entry per SKU.\n"
        f"URL: {url}\n"
        f"HTML (truncated, tags stripped of scripts/styles):\n"
        f"{_truncate(html, _MAX_HTML_CHARS_EXTRACT)}\n\n"
        "Respond with strict JSON only, a list of objects, each with keys: "
        "name, brand, sku, price (number or null), currency, pack_size, "
        "availability (one of: In stock, Partially in stock, Backordered, Special order, "
        "Out of stock, Unknown), description, "
        "image_urls (list of strings), specifications (object of string key/value "
        "pairs from the visible spec/attribute table; {} if none). "
        "Use null for anything not present. Never invent values."
    )
    try:
        raw = await _llm_async(
            [{"role": "user", "content": prompt}],
            purpose="extract",
            model=MODEL_DEFAULT,
        )
        items = json.loads(_extract_json(raw))
        if not isinstance(items, list):
            items = [items]
    except Exception:
        # Covers both an unparseable response (pre-existing) and the API call
        # itself raising (e.g. auth/network error) — found live during the
        # Net32 build when classify_with_llm's equivalent gap crashed a whole
        # run (BUILD_REPORT.md §10/§11); this is the same class of bug at the
        # tier-3 extraction fallback, fixed at the source instead of in every
        # caller.
        return []

    products = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            availability = Availability(item.get("availability")) if item.get("availability") else Availability.UNKNOWN
        except ValueError:
            availability = Availability.UNKNOWN
        price = item.get("price")
        try:
            price_f = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price_f = None
        images = item.get("image_urls") or []
        if isinstance(images, str):
            images = [images]
        specs = item.get("specifications") or {}
        if not isinstance(specs, dict):
            specs = {}
        try:
            products.append(
                Product(
                    variant_id=item.get("sku"),
                    name=item.get("name"),
                    brand=item.get("brand"),
                    sku=item.get("sku"),
                    category_path=category_path,
                    url=url,
                    crawl_url=url,
                    price=price_f,
                    currency=item.get("currency"),
                    price_status=PriceStatus.VISIBLE if price_f else PriceStatus.NOT_FOUND,
                    pack_size=item.get("pack_size"),
                    availability=availability,
                    description=item.get("description"),
                    specifications={str(k): str(v) for k, v in specs.items() if v not in (None, "")},
                    image_urls=[str(u) for u in images if u],
                    extraction_method="llm",
                )
            )
        except Exception:
            continue
    return products


extract_with_llm = extract_fallback_with_llm


async def extract_specifications_with_llm(html: str, url: str) -> dict[str, str]:
    """Fills Product.specifications (and, via 'Manufacturer Code'/'manufacturer_code',
    a hint for the real vendor SKU) when tier-1 structured data succeeded on the
    core fields but carries no spec table — the Net32 case (BUILD_REPORT.md §10):
    its Product JSON-LD has name/sku/brand/price/availability but no key-value
    specs. Only called when SiteAdapter.fill_missing_specifications_via_llm is
    set, so it never fires for Safco (0 LLM calls there stays true)."""
    prompt = (
        "Extract the key-value specification/attribute table from this ecommerce "
        "product page (e.g. Color, Material, Packaging, Size, Sterility, "
        "Manufacturer Code / Vendor Code). This is separate from the page's own "
        "structured data (JSON-LD), which this page already has for name/price/sku — "
        "only extract fields from the visible spec table or attribute list.\n"
        f"URL: {url}\n"
        f"HTML (truncated, tags stripped of scripts/styles):\n{_truncate(html, _MAX_HTML_CHARS_SPECS)}\n\n"
        "Respond with strict JSON only: a flat object of {\"Field Name\": \"value\"}. "
        "Omit any field not actually present. Never invent values. If nothing is "
        "found, respond with {}."
    )
    try:
        raw = await _llm_async(
            [{"role": "user", "content": prompt}],
            purpose="extract_specifications",
            model=MODEL_DEFAULT,
        )
        data = json.loads(_extract_json(raw))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v not in (None, "")}
    except Exception:
        return {}


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
    try:
        raw = await _llm_async(
            [{"role": "user", "content": prompt}],
            purpose="selector_repair",
            model=MODEL_REASONING,
        )
        data = json.loads(_extract_json(raw))
        return data.get("selector") or None
    except Exception:
        # This is called from recover_node itself — an unhandled exception
        # here would crash the run from inside the node whose entire job is
        # to prevent that. Degrade to "no repair suggested" instead.
        return None


def _extract_json(raw: str) -> str:
    """DeepSeek sometimes wraps JSON in markdown fences; strip them."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw
