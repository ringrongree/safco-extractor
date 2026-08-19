"""Canonical URL normalization. Canonical URL is the dedup + storage key,
not the crawl path. BUILD_SPEC.md §2.

`strip_query_params` (SiteAdapter.canonical, NET32_GENERALIZATION_KICKOFF.md
§2) exists because Net32 product URLs carry Algolia tracking params
(?queryID=, ?tsid=) that would otherwise fragment the same product into
multiple dedup keys across crawls/pages. Safco's canonical URLs never carry
query params, so this is a no-op there — off by default (SiteAdapter.canonical
defaults to strip_query_params=False)."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str, strip_query_params: bool = False) -> str:
    """Strip fragment and trailing slash so trivial variants collapse.
    Optionally also strip the query string entirely."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    query = "" if strip_query_params else parts.query
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def variant_key(canonical_url: str, sku: str | None) -> str:
    return f"{normalize_url(canonical_url)}::{sku or ''}"
