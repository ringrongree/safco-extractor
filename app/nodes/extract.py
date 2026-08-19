"""EXTRACT node — act: cascade. Structured data -> config selectors -> LLM,
stop at first success. BUILD_SPEC.md §9.

Tier 2 (selector) failing on a *first* attempt for a job reports stage_failed
so the graph routes to RECOVER for a one-shot selector-repair pass (cached in
memory for the rest of the run); a second attempt falls through to tier 3 (LLM)
so every job eventually produces a result or is deliberately dead-lettered.
"""
from __future__ import annotations

import sqlite3

from app.logging_setup import log_node_entry
from app.memory import RunMemory
from app.schemas import ExtractionMethod, LoopTrace, Product, PriceStatus, SiteAdapter
from app.state import GraphState
from app.tools.dedup import normalize_url
from app.tools.llm_extract import extract_fallback_with_llm, extract_specifications_with_llm
from app.tools.parse import css_first_text, extract_meta_keywords
from app.tools.structured_data import extract_canonical, extract_category_path, extract_json_ld_blocks
from app.tools.structured_data import structured_extract_product

_MANUFACTURER_CODE_KEYS = ("manufacturer code", "mfr code", "vendor code", "vendor sku")


def make_extract_node(memory: RunMemory, adapter: SiteAdapter, conn: sqlite3.Connection):
    async def extract_node(state: GraphState) -> dict:
        job = state["job"]
        html = state.get("html", "")
        assert job is not None
        log_node_entry("extract", job)

        # job.category_path (threaded down from the seed category job, see
        # enqueue.py) is the stable key RunMemory's per-category cap uses.
        # The page's own breadcrumb JSON-LD is richer (e.g. ["Dental Supplies",
        # "Sutures & surgical products", "Sutures"] vs just ["sutures-surgical-
        # products"]) but its first element is the site's generic catalog
        # root, not the seed category -- grouping/export logic that assumes
        # category_path[0] identifies the category would silently misgroup
        # every row under "Dental Supplies". Prefer the stable key.
        category_path = job.category_path or extract_category_path(extract_json_ld_blocks(html))

        # Tier 1: structured data (JSON-LD / microdata)
        products = structured_extract_product(html, job.url)
        if products:
            for p in products:
                p.category_path = category_path
                if p.url:
                    p.url = normalize_url(p.url, adapter.canonical.strip_query_params)
            method = "structured"

            # Tier-1 succeeded on the core fields but may still be missing
            # `specifications` (confirmed true for Net32's Product JSON-LD —
            # BUILD_REPORT.md §10: it has sku/name/price/availability, no spec
            # table). Gated behind the adapter flag so Safco's 0-LLM-call
            # regression is untouched (default False).
            if adapter.fill_missing_specifications_via_llm and not products[0].specifications:
                # extract_specifications_with_llm degrades to {} on any
                # failure (API error or unparseable response) rather than
                # raising, so an optional enrichment on top of an
                # already-successful tier-1 result can't crash the run.
                specs = await extract_specifications_with_llm(html, job.url)
                if specs:
                    mfr_code = next(
                        (v for k, v in specs.items() if k.strip().lower() in _MANUFACTURER_CODE_KEYS),
                        None,
                    )
                    for p in products:
                        p.specifications = specs
                        p.extraction_method = ExtractionMethod.LLM
                        if mfr_code:
                            p.sku = mfr_code
                    method = "llm"  # highest tier that contributed (kickoff §4.3)

            memory.log_trace(_trace(job, method, len(products)), conn)
            return {"extracted_products": products, "extraction_method": method, "stage_failed": None}

        # Tier 2: config selectors (repaired selectors from memory take precedence)
        selectors = {**adapter.selectors, **memory.repaired_selectors}
        name = css_first_text(html, selectors.get("product_name", "h1"))
        skus = extract_meta_keywords(html)
        canonical = normalize_url(
            extract_canonical(html) or job.url, adapter.canonical.strip_query_params
        )
        description = css_first_text(html, selectors.get("description", "p"))

        if name and skus:
            products = [
                Product(
                    variant_id=sku,
                    name=name,
                    sku=sku,
                    category_path=category_path,
                    url=canonical,
                    crawl_url=job.url,
                    price=None,
                    price_status=PriceStatus.NOT_FOUND,
                    description=description,
                    extraction_method=ExtractionMethod.SELECTOR,
                )
                for sku in skus
            ]
            memory.log_trace(_trace(job, "selector", len(products)), conn)
            return {"extracted_products": products, "extraction_method": "selector", "stage_failed": None}

        if job.attempts < 2:
            # First failure -> RECOVER escalates to headless and retries this node.
            # Second failure (still on tier 1+2) -> RECOVER repairs the selector
            # and retries once more. Only after both get a chance do we spend
            # an LLM call on full-page extraction.
            memory.log_trace(_trace(job, "selector_failed", 0), conn)
            return {"extracted_products": [], "extraction_method": None, "stage_failed": "extract"}

        # Tier 3: LLM extraction fallback (irregular layout, or repair didn't help)
        products = await extract_fallback_with_llm(html, job.url, category_path)
        for p in products:
            if p.url:
                p.url = normalize_url(p.url, adapter.canonical.strip_query_params)
        memory.log_trace(_trace(job, "llm", len(products)), conn)
        return {
            "extracted_products": products,
            "extraction_method": "llm" if products else None,
            "stage_failed": None if products else "extract",
        }

    return extract_node


def _trace(job, method: str, count: int) -> LoopTrace:
    return LoopTrace(
        job_id=job.job_id,
        url=job.url,
        decision=f"extraction tier '{method}' produced {count} row(s)",
        extraction_method=ExtractionMethod.NONE,
    )
