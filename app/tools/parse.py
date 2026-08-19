"""HTML parsing helpers (selectolax) + heuristic page-type classification.

Tier-2 of the extraction cascade (config selectors) and the heuristic half of
CLASSIFY both live here. BUILD_SPEC.md §9.
"""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from app.schemas import PageType


def css_first_text(html: str, selector: str) -> str | None:
    tree = HTMLParser(html)
    node = tree.css_first(selector)
    if node is None:
        return None
    text = node.text(strip=True)
    return text or None


def css_all_text(html: str, selector: str) -> list[str]:
    tree = HTMLParser(html)
    return [n.text(strip=True) for n in tree.css(selector) if n.text(strip=True)]


def css_first_attr(html: str, selector: str, attr: str) -> str | None:
    tree = HTMLParser(html)
    node = tree.css_first(selector)
    if node is None:
        return None
    return node.attributes.get(attr)


def css_all_attr(html: str, selector: str, attr: str) -> list[str]:
    tree = HTMLParser(html)
    out = []
    for n in tree.css(selector):
        v = n.attributes.get(attr)
        if v:
            out.append(v)
    return out


def extract_meta_keywords(html: str) -> list[str]:
    """SKUs live in <meta name="keywords">, server-rendered. Recon fact #confirmed."""
    content = css_first_attr(html, 'meta[name="keywords"]', "content")
    if not content:
        return []
    return [s.strip() for s in content.split(",") if s.strip()]


def discover_child_category_links(html: str, current_url: str, base_url: str) -> list[str]:
    """Sub-category links that are strict descendants of current_url. Restricting
    to descendants (not just any /catalog/ link) keeps the crawl scoped to the
    two seed categories instead of following the site's full global nav menu."""
    tree = HTMLParser(html)
    prefix = current_url.rstrip("/") + "/"
    found: set[str] = set()
    for node in tree.css("a"):
        href = node.attributes.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = base_url.rstrip("/") + href
        if href.startswith(prefix) and "?" not in href and "#" not in href:
            found.add(href.rstrip("/"))
    return sorted(found)


def discover_listing_product_links(html: str, selector: str) -> list[str]:
    """Tier-2 fallback for product links on a listing page (config selector)."""
    return css_all_attr(html, selector, "href")


def classify_heuristic(html: str, url: str) -> tuple[PageType | None, str]:
    """Cheap, deterministic page-type guess. Returns (type_or_None, reasoning).
    None means "ambiguous — escalate to LLM" per BUILD_SPEC.md §5.

    Note: Magento/Hyva emits an ItemList JSON-LD block for the sub-category
    nav too (name ends in "Subcategories"), so "contains an ItemList" alone is
    not a reliable LISTING signal — it must contain /product/ URLs."""
    tree = HTMLParser(html)

    has_product_ld = bool(re.search(r'"@type"\s*:\s*"ProductGroup"', html)) or bool(
        re.search(r'itemtype="http://schema\.org/Product"', html)
        and "/product/" in url
    )
    if "/product/" in url and has_product_ld:
        return PageType.PRODUCT, "URL matches /product/ and page contains Product/ProductGroup structured data"

    has_product_item_list = bool(
        re.search(r'"@type"\s*:\s*"ItemList"[^}]*?"itemListElement".*?/product/', html, re.DOTALL)
    ) or bool(re.search(r'"url"\s*:\s*"https?://[^"]*/product/', html))
    has_result_links = bool(tree.css_first("a.result")) or bool(tree.css_first("a.product-item-link"))
    if "/catalog/" in url and (has_product_item_list or has_result_links):
        return PageType.LISTING, "Page's ItemList JSON-LD (or grid anchors) contains /product/ URLs"

    if "/catalog/" in url:
        return PageType.CATEGORY, "URL under /catalog/ with no product-bearing ItemList or grid anchors detected"

    return None, "Could not confidently classify from URL pattern or DOM heuristics"
