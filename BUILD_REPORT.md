# BUILD_REPORT — Safco Catalog Extractor (POC)

Honest running log of what Claude Code built for this take-home, in the order
`BUILD_SPEC.md §18` prescribes. No overclaiming; stubs are marked as stubs.

## 1. The one unproven assumption (BUILD_SPEC.md §18 step 1)

**Required check**: run Crawl4AI against a live Safco product page and confirm
it renders the AJAX price/variant table that static fetch misses. Everything
downstream depends on this.

**Result: passed, with an important refinement discovered along the way.**

- Static fetch of a listing page (`/catalog/sutures-surgical-products/sutures`)
  returned **0** product links; headless render (crawl4ai, ~6s settle delay)
  returned all **6** (5 products + 1 "clearance-item" link). Confirms headless
  render genuinely surfaces content static fetch misses — the go/no-go gate
  BUILD_SPEC.md §18 step 1 required.
- On the product page `safco-surestitch-trade-sutures`: static fetch found 0
  `$`-formatted prices and a `Loading...` placeholder; headless found 14. So
  far this matches the recon doc exactly.
- **But** while building the extraction cascade's tier-1 (structured data), a
  `ProductGroup` JSON-LD block was found with a `hasVariant[]` array carrying
  all 15 variants' SKU/name/price/currency/availability — **and this block is
  present in the plain static HTML too**, not just the headless render. Same
  story one level up: every `/catalog/` page (including top-level category
  pages that visually show only sub-categories) server-renders a product-
  bearing `ItemList` JSON-LD block, plus a separate `ItemList` for the
  sub-category nav. Both survive a plain `httpx` GET.
- Prices were **visible**, not gated, for the session used in this build —
  contrary to the recon doc's "unable to process orders for your account"
  assumption. `price_status = gated` is still implemented and honored if that
  message appears in the fetched HTML, just not exercised by this run.

**Consequence**: the render-split design changed mid-build. See README.md §4
for the full writeup and the reasoning. Short version: every job now starts
`render_mode = static` (matching `config/safco.py`'s `start_render_mode`), and
headless is reserved for `RECOVER`'s escalate-render fallback rather than an
upfront rule for listing/product pages. This is a deliberate, evidence-based
deviation from BUILD_SPEC.md §8's literal categorization ("Headless — listing
grid, price, variant table, stock"), disclosed here rather than silently
diverging from the spec.

## 2. What's built and working

- **Full LangGraph spine** (`app/graph.py`) — 8 nodes (`next_job`, `fetch`,
  `classify`, `enqueue`, `extract`, `validate`, `store`, `recover`), cyclic
  routing, the crawl loop lives inside the graph (not an outer `while`).
  Verified with `recursion_limit=20000` for a full run.
- **Extraction cascade**, all three tiers implemented and independently
  exercised against real pages: structured data (JSON-LD/microdata) → config
  selectors → DeepSeek fallback. Tier 1 handled 100% of product extractions
  in the sample run (see §5) — tiers 2/3 are real code, verified in isolation
  (§4), but the happy path didn't need them on this site.
- **All 5 Pydantic schemas** frozen per §7, enforced everywhere; missing
  fields are explicit `None`/`[]`/`{}`, never fabricated.
- **Recovery**: retry, escalate-render, repair-selector, dead-letter — all
  four paths implemented in `app/nodes/recover.py`. Retry/escalate/dead-letter
  verified live against an unresolvable host (clean 3-attempt exhaustion, a
  `FailureRecord` written to SQLite — see §4). Repair-selector verified in
  isolation against real product HTML with a deliberately broken selector
  (DeepSeek suggested `h1.page-title`, a plausible Magento selector).
- **Governor**: per-category product cap and page budget both enforced and
  verified (a 2-product cap correctly stopped enqueueing further product jobs
  mid-run while still letting sub-category discovery continue).
- **Storage**: SQLite upsert on `(canonical url, sku)`, CSV/JSON export
  (combined + per-category), all exercised against real data.
- **FastAPI + Jinja2 UI**: one page, background crawl task, polling status,
  results table, CSV/JSON export links. Verified in a real browser via
  Playwright — see §6.
- **Rate limiting**: a real Fastly/Varnish 405 ("Error 54113") was hit live
  during development from rapid repeated testing against the site; fixed by
  sending a full browser-like header set (`Accept`, `Accept-Language`,
  `Accept-Encoding`) instead of a bare `User-Agent`. `request_delay_seconds`
  throttling (`RunMemory.throttle`) is also enforced between every fetch.

## 3. What's stubbed or faked

- **`specifications` and `alternatives`** on `Product` are always empty. No
  structured or reliably-selectable source for either was confirmed on Safco
  product pages in the time available (the "You may also like" carousel on
  product pages is Alpine.js-driven with per-user recommendation data, not a
  stable list of true alternative SKUs — extracting it would mean scraping a
  personalization widget, not real product data, so it was left out rather
  than faked).
- **Pagination** (`app/nodes/enqueue.py`, the `total_items > len(product_urls)`
  branch) is implemented against the `ItemList`'s `numberOfItems` field but
  **never fired** in the sample run or in ad-hoc testing — no category
  checked had more items than fit in one `ItemList` block. It has not been
  independently unit-tested with a synthetic multi-page fixture either. Flag
  this for the reviewer: it's plausible-looking code, not verified code.
- **LLM extraction fallback's page truncation** (`app/tools/llm_extract.py`,
  `_MAX_HTML_CHARS = 12000`) means on a genuinely large/irregular page, the
  LLM only sees a truncated, script/style-stripped slice. In the one live
  test run (§4), this recovered all 15 SKUs correctly but returned `price:
  None` / `availability: Unknown` for each — the pricing table didn't survive
  truncation. Tier 3 is a real safety net for identity fields (name/SKU), not
  a full substitute for tiers 1–2 on a large page.
- **`gated` price status** is implemented (checks for "unable to process
  orders" in the fetched HTML) but was never observed live — the session used
  for this build saw visible prices throughout.

## 4. Assumptions made / selectors guessed from live DOM

- Config selectors in `config/safco.py` (`product_name: h1`, `breadcrumbs:
  .breadcrumbs a, .breadcrumbs span`, `listing_product_links: a.result`,
  `sku_meta: meta[name="keywords"]`) were confirmed against live DOM on
  2026-08-18, but are tier-2 fallbacks only — the sample run never needed
  them (tier 1 always succeeded first).
- The site is Magento 2 + **Hyva theme** (Alpine.js, Tailwind-style utility
  classes), not the Luma theme the recon doc assumed. This didn't end up
  mattering much because the extraction strategy leans on JSON-LD (theme-
  independent) rather than Luma-specific CSS classes, but it's worth flagging
  since any selector-based (tier-2) work on this site should expect Hyva
  markup, not Luma's `.product-item-link` / `.price-box` conventions the
  recon doc's config sketch implied.
- `_guess_pack_size` (`app/tools/structured_data.py`) is a regex heuristic
  over the variant name (`"12/box"`-style suffixes) — confirmed working for
  both categories in the sample run (sutures: `"12/box"`; gloves: `"100/bag"`,
  `"200/box"`), 70% overall fill rate (see §5) — the miss rate is products
  priced by volume/weight rather than count, which the regex doesn't attempt.
- Recovery's `MAX_ATTEMPTS = 2` (≈3 tries before dead-letter) and the
  extract-node's attempts gate (`< 2` before falling to tier-3 LLM) are
  judgment calls sized for a POC, not derived from any measurement of
  Safco's actual transient-failure rate.

## 5. Sample run — numbers

Both categories, POC caps from `config/safco.py` (25 products/category), run
via the standalone script path (§5 of README.md), 2026-08-19:

| Metric | Value |
|---|---|
| Pages fetched | 66 (all static — zero headless fetches needed) |
| Products (variant rows) | 225 |
| — sutures-surgical-products | 109 rows |
| — gloves | 116 rows |
| Dead-letters | 0 |
| LLM calls | **0** |
| Elapsed | ~131s |
| Extraction method breakdown | 225/225 `structured` (100%) |

**Field-fill rate** (of 225 rows):

| Field | Filled | % |
|---|---|---|
| name | 225/225 | 100% |
| brand | 225/225 | 100% |
| sku | 225/225 | 100% |
| price | 225/225 | 100% |
| availability | 225/225 | 100% |
| description | 225/225 | 100% |
| pack_size | 157/225 | 70% |

`pack_size` is a regex heuristic over the variant name (`"12/box"`-style
suffixes) — the 30% miss rate is products whose variant name doesn't follow
that convention (e.g. bulk/jar items priced by volume, not count), not a
parsing failure. `specifications` and `alternatives` are 0% by design — see §3.

Committed under `data/samples/`: `safco_all_products.{csv,json}` (combined),
`safco_sutures-surgical-products.{csv,json}`, `safco_gloves.{csv,json}`.

## 6. UI verification

Verified in a real browser (Playwright against a live `uvicorn --reload`
instance), not just curl:

1. `GET /` — form pre-filled with the two categories + POC caps, renders
   correctly.
2. Submitted a scoped run (gloves, cap 2) via the actual form → `POST /run`.
3. Polled `/status/{run_id}` from the page's own JS until `done` — counters
   updated live (pages fetched, products found, LLM calls, dead-letters).
4. `/results/{run_id}` rendered a 9-row table (2 products × their variants),
   all fields populated, `extraction_method` badges correct.
5. `/export/{run_id}.csv` and `.json` both returned 200 with real content
   (6.4KB / 10.4KB respectively).

Two real bugs were caught and fixed during this pass, not just cosmetic:

- **Starlette's `TemplateResponse` signature**: the installed version
  (via FastAPI 0.141 / Starlette) requires `TemplateResponse(request, name,
  context)`, not the older `TemplateResponse(name, {"request": request,
  ...})` form. The old form doesn't raise at import time — it fails at
  request time with a confusing `TypeError: unhashable type: 'dict'` (the
  context dict ends up passed as the template *name*, then used as a cache
  key). Both routes in `app/main.py` were fixed.
- **Playwright + uvicorn `--reload` on Windows**: uvicorn's `--reload` (and
  `--workers > 1`) forces a Selector-based asyncio event loop on Windows
  (`uvicorn/loops/asyncio.py`, `use_subprocess=True` path) because its
  subprocess-based reload supervisor needs it — but Selector-based loops
  can't spawn subprocesses themselves, which is exactly what Playwright needs
  to launch a browser. `HeadlessFetcher` used to start the browser eagerly in
  `__aenter__`, so *every* run failed the moment `run_crawl` began, even
  runs that never touched headless. Fixed by making browser startup lazy
  (`app/tools/fetch_headless.py`, `_ensure_started`) — a run that stays
  static-only (the common case on this site) now works fine under
  `--reload`; only a run that genuinely needs the escalate-render fallback
  would still hit this on Windows. Documented as a known issue (§7) rather
  than fully solved, since the underlying uvicorn/Windows limitation is real.

## 7. Known issues

- **`--reload` on Windows still can't run a genuinely headless-dependent
  crawl.** The lazy-start fix (§6) covers the happy path; a run that actually
  needs `RECOVER`'s escalate-render fallback while running under `uvicorn
  --reload` on Windows will still fail with `NotImplementedError` from
  Playwright's subprocess launch. Workaround: drop `--reload` (or use
  `--workers 1` without `--reload`) for any run where headless might
  genuinely be needed. Not an issue on Linux/macOS.
- The Fastly rate-limit incident (§2) means aggressive concurrent testing
  against the live site (as happened repeatedly during this build) can
  trigger a temporary block. `request_delay_seconds` + full browser headers
  mitigate it but a production crawler at higher volume should expect to see
  this again and needs adaptive backoff (see README.md §9).
- `RunMemory` (frontier, visited set, all in-run state) lives entirely in the
  FastAPI process's memory. A server restart mid-run loses that run's
  progress — there's no checkpoint-based resumability wired up yet, even
  though LangGraph supports it. Documented as a scale-path item in README.md
  §9, not built for the POC per BUILD_SPEC.md §14.
- `classify_heuristic`'s LISTING-vs-CATEGORY signal is Safco/Magento-specific
  (regex over `ItemList` JSON-LD shape, `a.result`/`a.product-item-link`
  selectors). Porting this to a second supplier's site would need new
  heuristics, not a config tweak — the SiteAdapter carries selectors as data,
  but the classify heuristic itself is currently Python logic, not config.
  Worth flagging as a multi-site generalization gap.

## 8. For the reviewer, before the next iteration

1. The render-split deviation (§1) is the single most important thing to
   sanity-check — it's a real, reasoned departure from the spec's literal
   routing rule, not an oversight. Worth a second pair of eyes on whether the
   static-first design generalizes past Safco, since it depends on Magento
   emitting server-side JSON-LD, which not every Magento site is configured
   to do.
2. Tiers 2 and 3 of the extraction cascade, and 3 of 4 recovery actions, are
   verified in isolation against real content but not exercised by the actual
   sample run's happy path (§3). They're real, tested code — just not
   proven under the specific load of "the real crawl."
3. `specifications`/`alternatives` being empty is the biggest gap against the
   take-home brief's "capture as many of the following as possible" list —
   worth discussing whether the brochure-PDF route or the recs-widget route
   is worth pursuing in a next pass, or whether it's genuinely out of scope.
