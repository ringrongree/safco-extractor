"""Static fetch tool (httpx). For server-rendered content: category nav,
descriptions, meta/SKUs. BUILD_SPEC.md §8."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

HEADERS = {
    # A bare User-Agent triggered Fastly's synthetic 405 ("Error 54113") during
    # live testing 2026-08-18 — restored once a full browser-like header set
    # was sent. See BUILD_REPORT.md for the incident.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


@dataclass
class FetchResult:
    success: bool
    html: str = ""
    status_code: int | None = None
    final_url: str | None = None
    error: str | None = None


async def fetch_static(url: str, timeout: float = 30.0) -> FetchResult:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=HEADERS
        ) as client:
            resp = await client.get(url)
            return FetchResult(
                success=resp.status_code < 400,
                html=resp.text,
                status_code=resp.status_code,
                final_url=str(resp.url),
                error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
            )
    except httpx.HTTPError as exc:
        return FetchResult(success=False, error=str(exc))
