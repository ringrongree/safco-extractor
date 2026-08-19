"""Run-scoped memory: frontier queue, visited set, canonical map, governor.

BUILD_SPEC.md §6. Draft 1 = SQLite + in-process structures, all living on this
object for the lifetime of one crawl run. Bound into graph nodes via closure
(see graph.py) rather than carried inside the LangGraph state dict, because it
is shared/mutable run state, not per-iteration data.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from app.schemas import FailureRecord, Job, LoopTrace, Product, SiteAdapter


@dataclass
class RunMemory:
    adapter: SiteAdapter
    run_id: str

    # Hot state
    frontier: deque[Job] = field(default_factory=deque)
    visited: set[str] = field(default_factory=set)          # crawl URLs already fetched
    canonical_map: dict[str, str] = field(default_factory=dict)  # crawl_url -> canonical

    # Learned state (repaired selectors, cached for the run)
    repaired_selectors: dict[str, str] = field(default_factory=dict)

    # Index
    seen_variant_keys: set[str] = field(default_factory=set)  # (canonical_url, sku)
    category_product_counts: dict[str, int] = field(default_factory=dict)

    # Outputs
    products: list[Product] = field(default_factory=list)
    traces: list[LoopTrace] = field(default_factory=list)
    dead_letters: list[FailureRecord] = field(default_factory=list)

    # Governor counters
    pages_fetched: int = 0
    products_found: int = 0

    # Rate limiting
    _last_request_ts: float = 0.0
    _rate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Status / control
    status: str = "queued"  # queued | running | done | error
    error_message: str | None = None

    @staticmethod
    def _dedupe_key(job: Job) -> str:
        # A URL can legitimately be visited twice under different job types —
        # once as a category-nav probe, once as a listing/AJAX-grid probe.
        return f"{job.type.value}:{job.url}"

    def push(self, job: Job) -> bool:
        """Enqueue a job if not already visited/queued. Returns True if enqueued."""
        key = self._dedupe_key(job)
        if key in self.visited:
            return False
        if any(self._dedupe_key(j) == key for j in self.frontier):
            return False
        self.frontier.append(job)
        return True

    def pop(self) -> Job | None:
        if not self.frontier:
            return None
        return self.frontier.popleft()

    def mark_visited(self, job: Job) -> None:
        self.visited.add(self._dedupe_key(job))

    def category_key(self, category_path: list[str]) -> str:
        return category_path[0] if category_path else "uncategorized"

    def category_cap_reached(self, category_path: list[str]) -> bool:
        key = self.category_key(category_path)
        return self.category_product_counts.get(key, 0) >= self.adapter.max_products_per_category

    def increment_category_count(self, category_path: list[str], by: int = 1) -> None:
        key = self.category_key(category_path)
        self.category_product_counts[key] = self.category_product_counts.get(key, 0) + by

    def budget_exhausted(self) -> bool:
        return self.pages_fetched >= self.adapter.max_pages

    async def throttle(self) -> None:
        async with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_ts
            wait = self.adapter.rate_limit - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.monotonic()

    def add_product(self, product: Product) -> bool:
        """Upsert-in-memory on (canonical url, sku). Returns True if newly added."""
        key = f"{product.url}::{product.sku}"
        if key in self.seen_variant_keys:
            # replace existing (idempotent re-run semantics)
            for i, p in enumerate(self.products):
                if f"{p.url}::{p.sku}" == key:
                    self.products[i] = product
                    return False
        self.seen_variant_keys.add(key)
        self.products.append(product)
        self.products_found += 1
        return True

    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "pages_fetched": self.pages_fetched,
            "products_found": self.products_found,
            "dead_letters": len(self.dead_letters),
            "frontier_size": len(self.frontier),
            "category_counts": dict(self.category_product_counts),
            "error_message": self.error_message,
        }
