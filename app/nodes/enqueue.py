"""ENQUEUE node — act: navigate. Handles both category and listing page types,
matching the single ENQUEUE box in BUILD_SPEC.md §6's diagram.

Confirmed live on safcodental.com: every /catalog/ page — top-level category
rollups and leaf listings alike — server-renders a product-bearing ItemList
JSON-LD block even under a plain static fetch (Magento/Hyva also emits a
*separate* "Subcategories" ItemList for the sub-nav; structured_data.py
filters that one out). So a single static observation of any /catalog/ URL
carries both signals at once: sub-category links AND direct product URLs.
ENQUEUE therefore always checks for both, regardless of the CATEGORY/LISTING
label CLASSIFY assigned — there's no need for a separate headless "is there
really a grid here" probe the way an earlier draft of this node did (see
BUILD_REPORT.md for that dead end). Headless is reserved for RECOVER's
escalate-render path, for the rare page where structured data genuinely is
AJAX-only.
"""
from __future__ import annotations

import sqlite3
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

from app.logging_setup import log_node_entry
from app.memory import RunMemory
from app.schemas import (
    ExtractionMethod, FailureRecord, Job, JobType, LoopTrace, PageType,
    RecoveryStage, SiteAdapter,
)
from app.state import GraphState
from app.storage import insert_failure
from app.tools.dedup import normalize_url
from app.tools.parse import discover_child_category_links, discover_listing_product_links
from app.tools.structured_data import structured_extract_listing_urls, structured_extract_subcategory_urls


def _category_path_for(job: Job, adapter: SiteAdapter) -> list[str]:
    if job.category_path:
        return job.category_path
    # Seed jobs: derive the top-level category slug from the URL.
    for seed in adapter.categories:
        if job.url.rstrip("/") == seed.rstrip("/"):
            return [seed.rstrip("/").rsplit("/", 1)[-1]]
    return job.category_path


def _next_page_url(url: str, pagination, page: int) -> str:
    """Build the URL for `page`, branching on SiteAdapter.pagination.type:
    - query_param (Safco): url?{param}={page}
    - path_suffix (Net32): url + "/{page}" (page 1 is the bare url, so this
      is only ever called for page >= 2)
    """
    if pagination.type == "path_suffix":
        return url.rstrip("/") + f"/{page}"
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query[pagination.param] = [str(page)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _current_page(url: str, pagination) -> int:
    if pagination.type == "path_suffix":
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else 1
    parsed = urlparse(url)
    return int(parse_qs(parsed.query).get(pagination.param, ["1"])[0])


def make_enqueue_node(memory: RunMemory, adapter: SiteAdapter, conn: sqlite3.Connection):
    async def enqueue_node(state: GraphState) -> dict:
        job = state["job"]
        html = state.get("html", "")
        page_type = state.get("page_type")
        assert job is not None
        log_node_entry("enqueue", job)

        try:
            category_path = _category_path_for(job, adapter)
            queued: list[str] = []

            # --- Sub-category recursion (present on category-level pages) ---
            child_urls = structured_extract_subcategory_urls(html, job.url)
            if child_urls is None:
                child_urls = discover_child_category_links(html, job.url, adapter.base_url)
            for child_url in child_urls:
                child = Job(
                    url=child_url,
                    type=JobType.CATEGORY,
                    category_path=category_path,
                    parent_url=job.url,
                    depth=job.depth + 1,
                    render_mode=adapter.start_render_mode,
                )
                if not memory.budget_exhausted() and memory.push(child):
                    queued.append(child_url)

            # --- Direct product discovery (present on listing-bearing pages) ---
            if page_type == PageType.LISTING and not memory.category_cap_reached(category_path):
                result = structured_extract_listing_urls(html)
                method = "structured"
                total_items = None
                if result:
                    product_urls, total_items = result
                else:
                    product_urls = discover_listing_product_links(
                        html, adapter.selectors.get("listing_product_links", "a.result"),
                        adapter.base_url,
                    )
                    # Net32's grid links the same product multiple times with
                    # different Algolia tracking params (?tsid=...) — dedupe
                    # by canonical form so the per-category cap counts unique
                    # products, not URL variants of the same one.
                    if product_urls:
                        seen: set[str] = set()
                        deduped = []
                        for u in product_urls:
                            key = normalize_url(u, strip_query_params=True)
                            if key not in seen:
                                seen.add(key)
                                deduped.append(u)
                        product_urls = deduped
                    method = "selector"

                remaining = adapter.max_products_per_category - memory.category_product_counts.get(
                    memory.category_key(category_path), 0
                )
                for url in (product_urls or [])[: max(remaining, 0)]:
                    p_job = Job(
                        url=url,
                        type=JobType.PRODUCT,
                        category_path=category_path,
                        parent_url=job.url,
                        depth=job.depth + 1,
                        render_mode=adapter.start_render_mode,
                    )
                    if not memory.budget_exhausted() and memory.push(p_job):
                        queued.append(url)
                        memory.increment_category_count(category_path)

                # Pagination: only follow if the ItemList says there's more than
                # this page returned, and the category cap isn't already hit.
                if (
                    total_items
                    and product_urls
                    and total_items > len(product_urls)
                    and not memory.category_cap_reached(category_path)
                    and not memory.budget_exhausted()
                ):
                    current_page = _current_page(job.url, adapter.pagination)
                    next_url = _next_page_url(job.url, adapter.pagination, current_page + 1)
                    next_job = Job(
                        url=next_url,
                        type=JobType.LISTING,
                        category_path=category_path,
                        parent_url=job.parent_url,
                        depth=job.depth,
                        render_mode=adapter.start_render_mode,
                    )
                    if memory.push(next_job):
                        queued.append(next_url)

            memory.log_trace(
                LoopTrace(
                    job_id=job.job_id,
                    url=job.url,
                    page_type=page_type,
                    reasoning=f"enqueued {len(queued)} job(s)",
                    decision="enqueue",
                    render_mode=state.get("render_mode_used"),
                    extraction_method=ExtractionMethod.NONE,
                ),
                conn,
            )
        except Exception as exc:
            record = FailureRecord(
                job_id=job.job_id,
                url=job.url,
                stage=RecoveryStage.ENQUEUE,
                error=f"{type(exc).__name__}: {exc}",
                attempts=job.attempts,
                last_render_mode=job.render_mode,
            )
            memory.dead_letters.append(record)
            insert_failure(conn, record)

        return {}

    return enqueue_node
