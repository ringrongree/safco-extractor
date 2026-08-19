"""Net32 SiteAdapter — config as data, not code. NET32_GENERALIZATION_KICKOFF.md §4.1.

Deviates from the kickoff's draft config in three places, each backed by a live
finding recorded in BUILD_REPORT.md §10 rather than guessed:

- `start_render_mode: headless`, not `static`. Every static fetch tried during
  the §3 prove-first check (product page, listing page, listing page 2) came
  back as a Cloudflare JS-challenge 403, not partial/AJAX-only content. Static
  is unusable here, not merely insufficient.
- `rate_limit=6.0`, well above Safco's 2.0. Cloudflare blocked a headless
  fetch mid-run on the *second* sequential request during the prove step —
  this looks session/rate-sensitive, not a clean "browser vs. no browser"
  gate. Slower pacing is the mitigation available at this scope (stealth
  proxies etc. are out of scope per BUILD_SPEC.md §14's precedent).
- `selectors.listing_product_links` (not the kickoff draft's `product_link`
  key name) — matches the key `app/nodes/enqueue.py` actually reads. The
  kickoff's example used a different key name than the shipped code checks;
  using it verbatim would silently enqueue zero products on every run.

`fill_missing_specifications_via_llm=True`: the one piece of the Product
schema confirmed absent from Net32's product-page JSON-LD (specifications,
manufacturer/vendor code) — see BUILD_REPORT.md §10 for what was and wasn't
found in the structured data.
"""
from app.schemas import CanonicalConfig, PaginationConfig, RenderMode, SiteAdapter

NET32_ADAPTER = SiteAdapter(
    site_id="net32",
    base_url="https://www.net32.com",
    categories=[
        "https://www.net32.com/ec/gloves-infection-control-personal-products-l-509-569",
    ],
    allowed_paths=["/ec/"],
    rate_limit=6.0,
    max_products_per_category=12,  # POC cap — clean proof, not coverage
    # 1 page fetched per product (headless-only site) + 1 listing page + slack
    # for retries. The kickoff draft's max_pages=3 assumed Safco's cheap,
    # mostly-static page cost; on a headless-only site each product IS a page.
    max_pages=20,
    start_render_mode=RenderMode.HEADLESS,
    pagination=PaginationConfig(type="path_suffix"),
    canonical=CanonicalConfig(strip_query_params=True),  # strips ?queryID=, ?tsid=
    fill_missing_specifications_via_llm=True,
    selectors={
        # Deliberately partial — see NET32_GENERALIZATION_KICKOFF.md §4.1.
        # name/brand/price/availability/description/sku/pack_size all come
        # from the product-page JSON-LD (tier 1); this is the tier-2
        # fallback for listing-grid product links only.
        "listing_product_links": 'a[href*="-d-"]',
    },
)
