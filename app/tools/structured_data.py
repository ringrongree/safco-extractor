"""Tier 1 of the extraction cascade: JSON-LD / microdata. BUILD_SPEC.md §9.

Near-free, most reliable, tried first. Confirmed live on safcodental.com
(Magento 2 + Hyva theme, not Luma as the recon doc assumed — see BUILD_REPORT):

- Product pages ship a `ProductGroup` JSON-LD block with a `hasVariant[]` array
  carrying SKU + name + price + currency + availability for every variant.
  This is present even in the STATIC (non-headless) HTML.
- Listing pages ship an `ItemList` JSON-LD block with per-product name/sku/
  brand/price/availability/url, but only after headless render (the grid is
  AJAX-injected).
- `BreadcrumbList` gives the category hierarchy on both page types.
"""
from __future__ import annotations

import json
import re

from app.schemas import Availability, Product, PriceStatus

_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

_AVAILABILITY_MAP = {
    "instock": Availability.IN_STOCK,
    "limitedavailability": Availability.PARTIALLY_IN_STOCK,
    "backorder": Availability.BACKORDERED,
    "preorder": Availability.SPECIAL_ORDER,
    "outofstock": Availability.OUT_OF_STOCK,
    "discontinued": Availability.OUT_OF_STOCK,
}


def extract_json_ld_blocks(html: str) -> list[dict]:
    blocks: list[dict] = []
    for match in _LD_JSON_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            blocks.extend(b for b in data if isinstance(b, dict))
        elif isinstance(data, dict):
            # Some sites wrap multiple entities under "@graph"
            if "@graph" in data and isinstance(data["@graph"], list):
                blocks.extend(b for b in data["@graph"] if isinstance(b, dict))
            else:
                blocks.append(data)
    return blocks


def find_by_type(blocks: list[dict], type_name: str) -> dict | None:
    for b in blocks:
        if b.get("@type") == type_name:
            return b
    return None


def map_availability(uri: str | None) -> Availability:
    if not uri:
        return Availability.UNKNOWN
    key = uri.rsplit("/", 1)[-1].lower()
    return _AVAILABILITY_MAP.get(key, Availability.UNKNOWN)


def extract_canonical(html: str) -> str | None:
    m = re.search(r'<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    return m.group(1) if m else None


def extract_category_path(blocks: list[dict]) -> list[str]:
    """From BreadcrumbList, dropping 'Home' and the trailing product/leaf name."""
    crumbs = find_by_type(blocks, "BreadcrumbList")
    if not crumbs:
        return []
    items = sorted(crumbs.get("itemListElement", []), key=lambda x: x.get("position", 0))
    names = [i.get("name") for i in items if i.get("name")]
    if names and names[0].lower() == "home":
        names = names[1:]
    if len(names) > 1:
        names = names[:-1]  # drop the leaf (product name / current page)
    return names


def structured_extract_product(html: str, crawl_url: str) -> list[Product] | None:
    """Returns one Product per variant, or None if no ProductGroup/Product JSON-LD found."""
    blocks = extract_json_ld_blocks(html)
    group = find_by_type(blocks, "ProductGroup")
    canonical = extract_canonical(html) or crawl_url
    category_path = extract_category_path(blocks)

    if group and group.get("hasVariant"):
        brand = (group.get("brand") or {}).get("name")
        base_name = group.get("name")
        description = group.get("description") or None
        images = group.get("image") or []
        if isinstance(images, str):
            images = [images]

        products: list[Product] = []
        for variant in group["hasVariant"]:
            offer = variant.get("offers") or {}
            price_raw = offer.get("price")
            price = None
            price_status = PriceStatus.NOT_FOUND
            try:
                if price_raw is not None and float(price_raw) > 0:
                    price = float(price_raw)
                    price_status = PriceStatus.VISIBLE
            except (TypeError, ValueError):
                pass
            if "unable to process orders" in html.lower():
                price_status = PriceStatus.GATED
                price = None

            variant_name = variant.get("name") or base_name
            pack_size = _guess_pack_size(variant_name)

            products.append(
                Product(
                    variant_id=variant.get("sku"),
                    name=variant_name,
                    brand=brand,
                    sku=variant.get("sku"),
                    category_path=category_path,
                    url=canonical,
                    crawl_url=crawl_url,
                    price=price,
                    currency=offer.get("priceCurrency"),
                    price_status=price_status,
                    pack_size=pack_size,
                    availability=map_availability(offer.get("availability")),
                    description=description,
                    image_urls=images,
                    extraction_method="structured",
                )
            )
        return products

    # Fall back: a bare Product block with no variants (single-SKU product)
    single = find_by_type(blocks, "Product")
    if single and single.get("sku"):
        offer = single.get("offers") or {}
        price_raw = offer.get("price")
        price = None
        price_status = PriceStatus.NOT_FOUND
        try:
            if price_raw is not None and float(price_raw) > 0:
                price = float(price_raw)
                price_status = PriceStatus.VISIBLE
        except (TypeError, ValueError):
            pass
        images = single.get("image") or []
        if isinstance(images, str):
            images = [images]
        return [
            Product(
                variant_id=single.get("sku"),
                name=single.get("name"),
                brand=(single.get("brand") or {}).get("name"),
                sku=single.get("sku"),
                category_path=category_path,
                url=canonical,
                crawl_url=crawl_url,
                price=price,
                currency=offer.get("priceCurrency"),
                price_status=price_status,
                pack_size=_guess_pack_size(single.get("name")),
                availability=map_availability(offer.get("availability")),
                description=single.get("description") or None,
                image_urls=images,
                extraction_method="structured",
            )
        ]

    return None


def structured_extract_subcategory_urls(html: str, current_url: str) -> list[str] | None:
    """Sub-category URLs from a /catalog/ page's own ItemList JSON-LD.

    Magento/Hyva emits a distinct ItemList (name ending "Subcategories",
    items typed CollectionPage) alongside the product-bearing one on the
    same page. Confirmed server-rendered under a plain static fetch — no
    headless render needed. Scoped to strict descendants of current_url so
    the crawl can't wander into the site's unrelated top-level categories.
    """
    blocks = extract_json_ld_blocks(html)
    prefix = current_url.rstrip("/") + "/"
    for item_list in (b for b in blocks if b.get("@type") == "ItemList"):
        urls: list[str] = []
        for element in item_list.get("itemListElement", []):
            item = element.get("item") or {}
            if item.get("@type") != "CollectionPage":
                continue
            url = item.get("url")
            if url and url.startswith(prefix):
                urls.append(url)
        if urls:
            return urls
    return None


def structured_extract_listing_urls(html: str) -> tuple[list[str], int] | None:
    """Product URLs (+ total item count, for pagination) from a /catalog/ page's
    ItemList JSON-LD. Confirmed live: this block is server-rendered even in
    static HTML at every level (category rollups and leaf listings alike) —
    NOT the AJAX-only content the recon doc assumed. Magento/Hyva emits a
    *separate* ItemList for the sub-category nav (name ends in "Subcategories");
    that one is filtered out here since it has no /product/ URLs.
    Returns None if no product-bearing ItemList is present at all.
    """
    blocks = extract_json_ld_blocks(html)
    for item_list in (b for b in blocks if b.get("@type") == "ItemList"):
        urls: list[str] = []
        for element in item_list.get("itemListElement", []):
            url = element.get("url") or (element.get("item") or {}).get("url")
            if url and "/product/" in url:
                urls.append(url)
        if urls:
            total = item_list.get("numberOfItems") or len(urls)
            return urls, int(total)
    return None


def _guess_pack_size(name: str | None) -> str | None:
    """Heuristic: variant names often end in '.../box' style pack info, e.g.
    'SureStitch sutures chromic gut 4-0, C-6, 18", 12/box'."""
    if not name:
        return None
    m = re.search(r"(\d+\s*/\s*(?:box|bx|pk|pack|case|ea|each|bag|dispenser))", name, re.IGNORECASE)
    return m.group(1).strip() if m else None
