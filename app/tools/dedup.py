"""Canonical URL normalization. Canonical URL is the dedup + storage key,
not the crawl path. BUILD_SPEC.md §2."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    """Strip fragment and trailing slash so trivial variants collapse."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def variant_key(canonical_url: str, sku: str | None) -> str:
    return f"{normalize_url(canonical_url)}::{sku or ''}"
