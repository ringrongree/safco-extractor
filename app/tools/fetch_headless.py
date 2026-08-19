"""Headless fetch tool (crawl4ai / Playwright). Mandatory for AJAX-rendered
content: listing product grid, price, variant table, stock. BUILD_SPEC.md §8.

Proven live 2026-08-18 against safcodental.com: static fetch of a listing page
returns 0 product links; headless render (with a settle delay) returns the full
grid. See BUILD_REPORT.md for the full proof log.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

# Empirically, Safco's AJAX grid/price widgets finish painting within ~4-6s of
# navigation. No official "network idle" signal was reliable across page types
# (recs widgets keep polling), so a fixed settle delay is used instead of wait_for.
SETTLE_DELAY_SECONDS = 6.0


@dataclass
class FetchResult:
    success: bool
    html: str = ""
    status_code: int | None = None
    error: str | None = None


class HeadlessFetcher:
    """Owns one browser instance for the lifetime of a crawl run, started
    lazily on first use rather than in __aenter__.

    Two reasons: (1) most runs on this site never escalate to headless at all
    (structured data on the static fetch covers it — see BUILD_REPORT.md), so
    eagerly launching Playwright would cost several seconds of browser startup
    for nothing; (2) launching Playwright requires an event loop that supports
    subprocess creation, which uvicorn's `--reload` mode does NOT provide on
    Windows (it forces a Selector-based loop for its subprocess-based
    reloader). Deferring startup means a run that stays static-only works
    fine even under `--reload` on Windows; only a run that genuinely needs
    escalation would hit that limitation. See BUILD_REPORT.md.
    """

    def __init__(self) -> None:
        self._crawler: AsyncWebCrawler | None = None
        self._start_lock = asyncio.Lock()

    async def __aenter__(self) -> "HeadlessFetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        if self._crawler is not None:
            await self._crawler.__aexit__(*exc)
            self._crawler = None

    async def _ensure_started(self) -> None:
        if self._crawler is not None:
            return
        async with self._start_lock:
            if self._crawler is None:
                crawler = AsyncWebCrawler(config=BrowserConfig(headless=True))
                await crawler.__aenter__()
                self._crawler = crawler

    async def fetch(self, url: str, timeout_ms: int = 60000) -> FetchResult:
        await self._ensure_started()
        assert self._crawler is not None
        config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            delay_before_return_html=SETTLE_DELAY_SECONDS,
            page_timeout=timeout_ms,
        )
        try:
            result = await self._crawler.arun(url=url, config=config)
            return FetchResult(
                success=bool(result.success),
                html=result.html or "",
                status_code=getattr(result, "status_code", None),
                error=None if result.success else "crawl4ai reported failure",
            )
        except Exception as exc:  # crawl4ai raises broad RuntimeError on wait/timeout issues
            return FetchResult(success=False, error=str(exc))
