"""Safco Dental Supply SiteAdapter — config as data, not code.

Shipped as a Python module rather than YAML: BUILD_SPEC.md §12 explicitly allows
"safco.yaml (or .py)", and going .py avoids adding PyYAML to the dependency list
(BUILD_SPEC.md §4 locks the stack; the kickoff prompt asks before adding anything
not on that list). No Safco-specific strings live in app/nodes/* — they all come
from this SiteAdapter instance.
"""
from app.schemas import PaginationConfig, RenderMode, SiteAdapter

SAFCO_ADAPTER = SiteAdapter(
    base_url="https://www.safcodental.com",
    categories=[
        "https://www.safcodental.com/catalog/sutures-surgical-products",
        "https://www.safcodental.com/catalog/gloves",
    ],
    allowed_paths=["/catalog/", "/product/"],
    rate_limit=2.0,               # request_delay_seconds
    max_products_per_category=25, # POC cap; raise for a full run
    max_pages=200,
    start_render_mode=RenderMode.STATIC,
    pagination=PaginationConfig(type="query_param", param="p"),
    selectors={
        # Tier-2 fallback selectors — tried only when JSON-LD / microdata (tier 1)
        # doesn't cover a field. Confirmed against live DOM 2026-08-18.
        "sku_meta": 'meta[name="keywords"]',
        "product_name": "h1",
        "breadcrumbs": ".breadcrumbs a, .breadcrumbs span",
        "canonical_link": 'link[rel="canonical"]',
        "listing_product_links": "a.result",
        "description": '[itemprop="description"], .product.attribute.description',
    },
)
