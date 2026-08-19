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

from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

from app.memory import RunMemory
from app.schemas import ExtractionMethod, Job, JobType, LoopTrace, PageType, RenderMode, SiteAdapter
from app.state import GraphState
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


def _next_page_url(url: str, param: str, page: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query[param] = [str(page)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def make_enqueue_node(memory: RunMemory, adapter: SiteAdapter):
    async def enqueue_node(state: GraphState) -> dict:
        job = state["job"]
        html = state.get("html", "")
        page_type = state.get("page_type")
        assert job is not None

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
                render_mode=RenderMode.STATIC,
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
                    html, adapter.selectors.get("listing_product_links", "a.result")
                )
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
                    render_mode=RenderMode.STATIC,
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
                parsed = urlparse(job.url)
                current_page = int(parse_qs(parsed.query).get(adapter.pagination.param, ["1"])[0])
                next_url = _next_page_url(job.url, adapter.pagination.param, current_page + 1)
                next_job = Job(
                    url=next_url,
                    type=JobType.LISTING,
                    category_path=category_path,
                    parent_url=job.parent_url,
                    depth=job.depth,
                    render_mode=RenderMode.STATIC,
                )
                if memory.push(next_job):
                    queued.append(next_url)

        memory.traces.append(
            LoopTrace(
                job_id=job.job_id,
                url=job.url,
                page_type=page_type,
                reasoning=f"enqueued {len(queued)} job(s)",
                decision="enqueue",
                render_mode=state.get("render_mode_used"),
                extraction_method=ExtractionMethod.NONE,
            )
        )

        return {}

    return enqueue_node
