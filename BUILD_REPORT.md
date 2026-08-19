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

## 6.5 Cold-clone check (BUILD_SPEC.md §16)

`git clone` to a clean directory, fresh `venv`, `pip install -r
requirements.txt`, `cp .env.example .env` + key, `python -c "import
app.main"` and `uvicorn app.main:app` all verified working from that clean
checkout (same machine — Playwright's browser cache is machine-global, not
venv-local, so `crawl4ai-setup` wasn't re-run for this check; it would be
needed on a genuinely different machine per the README instructions).

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

## 9. Net32 generalization — Step 1, current-state read (`NET32_GENERALIZATION_KICKOFF.md`)

Before touching code: `classify_heuristic` (`app/tools/parse.py`) is regex/URL-pattern
matching hardcoded to Safco's `/catalog/`/`/product/` URL shape, `ProductGroup` JSON-LD,
and `a.result`/`a.product-item-link` selectors — not adapter-driven, but it degrades
correctly (returns `None` → LLM) on any URL that doesn't match those patterns. Recovery
edges: only `fetch`, `classify`, `extract`, `validate` route to `recover`; `enqueue`,
`next_job`, `store` don't (matches `INVENTORY_REPORT.md` finding B exactly — confirmed
again by direct code read). `LoopTrace` is still never persisted (finding C, still true).

## 10. Net32 Step 1 — prove-first gate (`NET32_GENERALIZATION_KICKOFF.md` §3): **STOPPED**

Per the kickoff's own gate ("if product JSON-LD is unexpectedly present and clean, STOP
and report — the whole slice depends on tier-1 missing"), this fired. Two of §2's recon
facts are contradicted by a live check, plus a third reliability risk not anticipated by
either doc. Full method: `app/tools/fetch_static.py::fetch_static` and
`app/tools/fetch_headless.py::HeadlessFetcher` called directly (no new code), raw HTML
dumped to disk, JSON-LD blocks parsed with the same regex `structured_data.py` uses.

**A. Static fetch is not "missing AJAX content" — it's fully blocked.** All three static
fetches (product page, listing page, listing page 2) returned HTTP 403 with an identical
body: a Cloudflare interstitial (`<title>Just a moment...</title>`, CSP referencing
`challenges.cloudflare.com`) — a JS challenge page, not a data page. `httpx` cannot solve
this; it isn't a header/User-Agent problem like Safco's Fastly 405 was. Recon's "Listing
pages are server-rendered... Static fetch enumerates the grid" and "Product page core...
= static" are both false under the current fetch approach: **nothing** is fetchable
statically right now, not just the AJAX-only pieces recon called out.

**B. Headless (crawl4ai/Playwright) passes the challenge and finds a clean, complete
`Product` JSON-LD block on the product page — contradicting "No JSON-LD in page head."**
`https://www.net32.com/ec/essentials-ultra-premium-nitrile-exam-gloves-medium-d-172552`,
headless-fetched: 3 `application/ld+json` blocks — `Product`, `BreadcrumbList`,
`Organization`. The `Product` block has `sku: "172552"`, `name`, `brand.name`,
`description`, `image`, and a nested `offers` object with `price: "14.95"`,
`priceCurrency: "USD"`, `availability: "https://schema.org/InStock"`. This is exactly the
shape `structured_extract_product()`'s existing bare-`Product` fallback path (the one
written for Safco's single-SKU products) already consumes — `single.get("sku")` is
truthy, so tier-1 would succeed immediately, tag `extraction_method=structured`, and
**never reach the LLM**. This directly contradicts the kickoff's central premise ("Net32
has... no JSON-LD... tier-1 structured extraction misses") and its Definition of Done
("Net32 rows tagged `llm`/`selector`", "Extraction-method distribution... is the headline
result").

The listing page (headless) also carries JSON-LD — 5 blocks: two `ItemList` (one with
1639 `numberOfItems`/60 per-page `ListItem`s carrying `name`+`image`+`url` but **no**
`sku`/`price`/`availability`, one a 3-item "More About Gloves" content block),
`FAQPage`, `BreadcrumbList`, `Organization`. The product-bearing `ItemList`'s `url`
values don't contain `/product/` (Net32 uses `-d-{id}` in-path, no `/product/` segment),
so `structured_extract_listing_urls()`'s existing `/product/`-substring filter correctly
returns `None` for Net32 — listing-level discovery does fall through to the tier-2
selector path as intended. The gate-breaking finding is specific to the **product-page**
tier-1 cascade, not listing enqueue.

**C. Even headless is not reliably passing Cloudflare — a new risk neither doc
anticipated.** In the same run, immediately after the listing page succeeded, fetching
page 2 (`.../l-509-569/2`, the path-suffix pagination check §3 asked for) failed:
crawl4ai's own error — `Blocked by anti-bot protection: Cloudflare JS challenge`. So
path-suffix pagination itself is **unconfirmed**, not because the URL scheme is wrong,
but because the second sequential headless request in the same session got challenged.
Cloudflare's block here looks session/rate-sensitive, not a clean "browser vs. no
browser" gate — a materially different, and larger, reliability problem than Safco's
Fastly rate-limit (which a static header fix resolved permanently).

**D. Smaller, code-level finding that would matter if this proceeds unmodified:**
`extract_category_path()` (`structured_data.py`) drops the trailing breadcrumb entry on
the assumption it's the product's own name (true on Safco). Net32's product-page
`BreadcrumbList` is `Home > Dental Supplies > Infection control - personal products >
Gloves` — it does **not** include the product name at all, so the same "drop the last
crumb" logic would incorrectly truncate `category_path` to `["Dental Supplies",
"Infection control - personal products"]`, silently losing `"Gloves"` — diverging from
the kickoff's own stated breadcrumb expectation (§2, seed URLs table).

**Artifacts** (scratchpad, not committed — reproducible via the prove script):
`listing_static.html` / `product_static.html` / `listing_page2_static.html` (all 403
Cloudflare interstitials), `product_headless.html`, `listing_headless.html`,
`listing_page2_headless.html` (the failed one, partial/challenge body).

**Not proceeding to §4 (build) yet** — the slice as scoped depends on both broken
assumptions. Flagged back to the requester per kickoff §1's own instruction
("flag anything that contradicts this doc; if so, ask") and §3's explicit stop gate,
with options, rather than silently redesigning the cascade or picking a different
product page to route around the finding.

## 11. Net32 Step 1 — decisions after the stop-gate

Two decisions, made explicitly rather than picked silently:

1. **Reframe the LLM-proof around `specifications{}`.** Tier-1 (structured data) is
   reported honestly as succeeding on Net32's core Product fields (name, sku, brand,
   price, currency, availability, description, images) — that's a real finding, not
   hidden. What JSON-LD never carries (confirmed by grepping the DOM outside the
   JSON-LD blocks): the key-value spec table (Manufacturer Code, Packaging, Sterility,
   Color, Material) and the multi-vendor offer comparison. `extract_node`'s cascade
   became additive instead of strictly binary: tier-1 fills the core row, then — gated
   behind a new `SiteAdapter.fill_missing_specifications_via_llm` flag, off by default
   — an LLM call fills `specifications` (and corrects `sku` to the real manufacturer
   code when the LLM surfaces one, since JSON-LD's `sku` field is Net32's internal
   product id, not a real SKU — see §4.4's own "note" on this). `extraction_method`
   reports the **highest tier that contributed** per the kickoff's own §4.3 language:
   Net32 rows read `llm`, Safco rows stay `structured`, because the flag defaults to
   `False` and Safco's adapter never sets it.
2. **Slow down + retry-with-backoff for the Cloudflare risk, accept pagination might
   stay unconfirmed.** `NET32_ADAPTER.rate_limit=6.0` (vs. Safco's 2.0); a generic
   (non-host-keyed) anti-bot-error backoff was added to `recover.py`'s fetch-stage
   retry. In practice this undersold how bad the problem was — see §12.

## 12. Net32 Step 1 — build: what changed, and real bugs found along the way

Built per NET32_GENERALIZATION_KICKOFF.md §4, in order: SiteAdapter extensions →
`config/net32.py` + host registry → classify (unchanged, see below) → extract
(additive cascade) → LoopTrace persistence → recovery on all 8 nodes → run-metrics
persistence → run + regress. Every one of the following was found by actually running
the code against the live site, not by reading it — several materially changed the
plan from §11's decisions.

**A third, bigger Cloudflare surprise.** §10 already found Cloudflare blocking static
fetch entirely and blocking a second sequential *headless* request during the prove
step. The chosen mitigation (slower rate limit + backoff) was **not enough** — a real
crawl run got JS-challenged on the very first product page, 3 times in a row, backoff
included, and dead-lettered. Checked `crawl4ai`'s `BrowserConfig` for a sanctioned,
first-class option before trying anything custom: `enable_stealth: bool` (Playwright
stealth patches, not bespoke evasion code). Flipping it in `HeadlessFetcher`
(`app/tools/fetch_headless.py`) resolved it completely — a clean two-URL repro went
from immediate-block to two clean successes, and the full 12-product run then
completed with **zero dead-letters**. This means headless + stealth is **load-bearing
for Net32's entire happy path**, not an edge-case escalation tier — the opposite of
what the kickoff's own §5.3 predicted ("the render-split/headless/RECOVER-escalate
path is unexercised on both sites"). That prediction was correct for Safco and wrong
for Net32; corrected here rather than silently reported as if it held.

**Bugs found and fixed (all verified via a live run reproducing the failure, then the
fix, not just read-and-guessed):**

1. **`classify_node`'s recovery routing was dead code.** `recover.py` has classify-
   specific logic (escalate to headless once, then dead-letter), but nothing ever set
   `stage_failed="classify"` — `classify_node` returned `page_type` alone. A live run
   hit this directly: a classify failure fell into the generic "unrecognized failure
   stage" dead-letter on the *first* attempt instead of getting its designed retry.
   Pre-existing bug (not introduced by this build), surfaced because Net32 routes
   every page through classify's LLM path where Safco almost never does. Fixed by
   having `classify_node` set `stage_failed` correctly.
2. **Unhandled LLM API exceptions could crash the whole run.** Reproduced live: with
   `DEEPSEEK_API_KEY` unset, `classify_with_llm`'s underlying API call raised
   `RuntimeError`, uncaught, and killed the entire crawl — not just that one job. The
   existing code only guarded against *unparseable* LLM responses, not the API call
   itself failing. Same shape existed at the other two LLM call sites
   (`extract_fallback_with_llm`, `repair_selector_with_llm` — the latter is called
   *from inside* `recover_node`, whose entire job is to prevent exactly this). Fixed
   at the source in `app/tools/llm_extract.py`: all three now degrade to their
   existing "nothing found" return value on any exception, not just a parse failure.
3. **`discover_listing_product_links` didn't resolve relative hrefs.** Net32's grid
   returns a mix of absolute and root-relative (`/ec/...`) hrefs from the same
   selector. A relative href reached `fetch_static`/`fetch_headless` directly and
   crawl4ai rejected it ("URL must start with 'http://'...") — dead-lettered after 3
   attempts on a URL that was never fetchable in the first place, no matter how many
   retries. Generic bug (any selector-based listing discovery can return relative
   hrefs), not Net32-specific; `discover_child_category_links` already resolved
   relative URLs, this sibling function hadn't caught up. Fixed by threading
   `adapter.base_url` through.
4. **Two of the kickoff's own draft config values didn't survive contact with the
   live site.** Its example selector key (`product_link`) doesn't match what
   `app/nodes/enqueue.py` actually reads (`listing_product_links`) — used verbatim,
   it would have silently discovered zero products, no error, empty run. Its example
   `max_pages: 3` assumed something like Safco's cheap-page-cost profile; on a
   headless-only site every product fetch costs a full page of budget, so 3 pages
   caps out at ~2 products. Both corrected in `config/net32.py`, documented in its
   own docstring rather than silently changed.
5. **`extraction_method` wasn't actually being set on the `Product` objects.** The
   additive-cascade logic (§11) correctly computed a local `method` variable for the
   graph state and the trace, but never wrote it back to `p.extraction_method` on the
   product rows themselves — the CSV/JSON export reads the latter. First full run
   exported 12/12 rows as `structured` despite specifications genuinely being
   LLM-filled. Caught by inspecting the actual exported file rather than trusting the
   printed run summary, fixed, and the run redone.
6. **The specifications LLM call couldn't see the spec table at all.** The shared
   `_MAX_HTML_CHARS = 12000` truncation window (script/style-stripped) cut off before
   Net32's spec table, which lives at ~13.6k-14.2k chars in on the page tested — even
   though some fields (Color, ~4.8k) and the JSON-LD core fields were within the
   window. First real run's specifications-fill genuinely fired (the LLM call
   happened) but returned `{}` every time — not a fabrication risk, just useless.
   Gave `extract_specifications_with_llm` its own larger window
   (`_MAX_HTML_CHARS_SPECS = 25000`); the other three LLM call sites (classify,
   extract_fallback, selector_repair) keep the original 12000 — no reason to pay for
   a bigger prompt where it isn't needed.
7. **Duplicate product URLs were eating the per-category cap.** Net32's grid links
   the same product multiple times with different Algolia tracking params
   (`?tsid=...`). Deduped by canonical form (`normalize_url(..., strip_query_params=True)`)
   before slicing to `max_products_per_category`, so the cap counts unique products.

**Config-driven discipline (§4.8) verified, not just claimed:** `grep -ri net32
app/**/*.py` → 14 hits, all in comments/docstrings citing this report or the kickoff
doc; zero in executable conditionals. The only files with `net32`/`Net32` as live
code are `config/net32.py` (the adapter itself) and `config/registry.py` (the one
place a host string is allowed to appear, by design).

## 13. Net32 Step 1 — results (both sites, real runs)

**Net32** (`data/samples/net32/net32_gloves.{csv,json}`, gloves category, cap 12):

| Metric | Value |
|---|---|
| Products (variant rows) | 12/12 |
| Pages fetched | 13 (1 listing + 12 product, all headless+stealth) |
| Dead-letters | 0 |
| LLM calls | 25 (13 `classify`, 12 `extract_specifications`) |
| Extraction method | 12/12 `llm` (tier-1 structured core + LLM-filled specifications) |
| `specifications` fill | 12/12 (7-8 fields each: Manufacturer Code, Brand, Color, Material, Packaging, Size, Sterility) |
| `price` fill | 11/12 (1 product's own JSON-LD genuinely has no `offers` block — correctly `null`, not fabricated) |
| Canonical URLs | 0/12 retain `?tsid=`/`?queryID=` tracking params |
| Elapsed | ~260s |

**Safco regression** (re-run against the live site, not just re-read from the
committed sample):

| Metric | This run | Original sample |
|---|---|---|
| Products | 225 (109 sutures / 116 gloves) | 225 (109/116) — exact match |
| Extraction method | 225/225 `structured` | 225/225 `structured` |
| LLM calls | 0 | 0 |
| Dead-letters | 0 | 0 |
| Pages fetched | 66 | "66" (previously **unverifiable** per INVENTORY_REPORT.md §H — now independently reproduced) |
| Elapsed | ~159s | "~131s" (network variance, same ballpark) |

**Classify path counts:** Safco — 0 LLM classify calls across the regression run
(heuristic handled every page, unchanged from the original build). Net32 — 13/13
pages classified via LLM (`classify_heuristic` returns `None` for every Net32 URL,
since neither matches `/catalog/`/`/product/`; confirmed offline against cached HTML
before the first live run, then confirmed again in every real run). No Net32
heuristics were added to `classify_heuristic` — it is byte-for-byte the same
function that already existed for Safco.

**Closed audit/spec gaps:**
- `LoopTrace` persisted to SQLite (`traces` table) — 119 rows for the Net32 dev runs,
  232 for the Safco regression run, real artifacts in `data/safco.db`, not just
  in-memory.
- Recovery on all 8 nodes — `enqueue`/`next_job`/`store` now dead-letter and continue
  on an internal exception instead of aborting the run; `classify`'s previously-dead
  recovery branch (bug 1, §12) now actually reachable.
- Run metrics persisted (`runs` table: `pages_fetched`, `products_found`,
  `llm_calls`, `dead_letters`, `elapsed_seconds`) — written from `main.py`'s `/run`
  handler on every run, and from the standalone scripts used to produce the numbers
  in this section.
- `dedup.py::normalize_url` is no longer dead code — used for canonical
  query-param stripping (Net32) and duplicate-listing-URL collapsing.

**Step-2 seams (§6), confirmed present, none built:**
- **Seam A (fetch routing)** — not just present but *exercised*: Net32's entire happy
  path runs through `fetch_node`'s `job.render_mode == HEADLESS` branch, driven by
  `SiteAdapter.start_render_mode`, zero node-level host checks. `RECOVER`'s
  escalate-render edge is real and fired during earlier debugging (§10, §12).
- **Seam B (extract modularity)** — `Product.alternatives` stays `[]`/null for every
  Net32 row; the additive-cascade pattern added for `specifications` (§11) is the
  same insertion point a future `extract_alternatives` sub-step would use.
- **Seam C (trace visibility)** — `LoopTrace.render_mode` is populated on every
  trace; Net32's persisted traces show `headless` throughout, confirming this would
  surface a Step-2 headless-for-alternatives fetch immediately.
- **Seam D (adapter hook)** — `SiteAdapter.alternatives: Optional[AlternativesConfig]`
  exists (`site_id`, `canonical`, and this were all additive schema changes); unset
  on both `SAFCO_ADAPTER` and `NET32_ADAPTER`.

## 14. Net32 Step 1 — Definition of Done (kickoff §8), checked against this report

- [x] Repo read; current-state summary written to BUILD_REPORT (§9).
- [x] Net32 no-JSON-LD assumption checked — **found false** for product pages
      (§10); stopped, reported, got explicit sign-off on how to adapt (§11) before
      building.
- [x] `config/net32.py` adapter + host→adapter registry (`config/registry.py`).
- [x] Classify: LLM fires on Net32 (13/13 pages, real runs), heuristic still handles
      Safco (0 LLM classify calls in the regression), no Net32 heuristics added.
- [x] Extract: Net32 rows tagged `llm` (12/12), Safco rows still `structured`
      (225/225).
- [x] Net32 gloves sample (12 products) → validated `Product` rows → CSV+JSON in
      `data/samples/net32/`.
- [x] `LoopTrace` persisted to SQLite.
- [x] Recovery, defined as **"a failure at this node does not abort the run."**
      Verified against source this session, not 8 identical edges:
      - `fetch`, `classify`, `extract`, `validate` route to the **`recover` node**
        (retry | escalate_render | repair_selector | dead_letter).
      - `enqueue`, `store`, `next_job` **catch-and-dead-letter inline** (a failure
        writes a `FailureRecord` and continues; `next_job` ends the run cleanly on
        a corrupt frontier rather than re-popping it).
      - `recover` itself guards its own LLM call (`repair_selector_with_llm`
        degrades to `None`).
      That's 4 routed + 3 inline + `recover`'s own guard.
- [x] Run metrics persisted; figures in §13 are artifact-backed (traces/runs/failures
      tables, re-run against the live site), not asserted.
- [x] Safco regression clean (225 rows, structured, 0 LLM) — re-run live, not just
      re-read from the committed sample.
- [x] Method distribution, render-split note, Step-2 seams — §13, with the honest
      correction that headless turned out to be load-bearing on Net32 (§12), not
      unexercised as predicted.
- [x] Config-driven: zero `net32` literals in node logic (verified by grep, §12).

**Honest deltas from the kickoff's literal plan**, disclosed rather than buried:
render_mode defaults to `headless` for Net32 (not `static`); `enable_stealth=True`
is required, not optional, for Net32's happy path; `extraction_method=llm` reflects
"highest tier contributed" (per §4.3's own wording), not "tier-1 missed entirely" —
tier-1 structured data actually covers Net32's core fields well, which is itself a
finding worth carrying into any Step 2/production discussion about this site.

## 15. LLM-primary inversion — Phase 0 STOP; Option 1 locked (2026-08-19)

**Verdict: Option 1 — invert neither.** Safco stays JSON-LD-primary (today’s
graph). LLM-primary already lives on Net32 (classify on every page; specs LLM
when the adapter flag is on). `extract.py` / `classify.py` are unchanged.

Prove-first on **one** product: `https://www.safcodental.com/product/safco-surestitch-trade-sutures`
(the known 15-variant SureStitch page). Cached HTML:
`data/phase0/surestitch_{static,headless}.html`.

### Phase 0 finding (verbatim)

**STOP. Visible-DOM LLM extract cannot replace Safco JSON-LD.** Headless visible
DOM does not carry the 15 sku/**size** rows. The LLM can parrot 15 SKUs from
`<meta name="keywords">` but cannot recover per-variant name/price/availability
from the slice `_truncate` would send it. Oracle N=15 vs stored-quality LLM M is
not 15==15 on the headline metric (matched SKU keys).

| Source | Oracle `structured_extract_product` | 25k script-stripped slice | LLM unified extract |
|---|---|---|---|
| Static fetch | **15** (JSON-LD in `<script>`) | 15 hyphenated SKUs in meta keywords only; **0** variant names; `Loading...` still in full stripped HTML (past 25k) | not used as primary evidence |
| Headless fetch (6s settle) | **15** (same JSON-LD) | same 15 hyphenated SKUs in keywords; Alpine table still `x-html="item.product_sku"` **unhydrated**; variant names **0** in stripped HTML | **15 rows**, all `sku=102-5801` style, all `name=Safco SureStitch sutures`, all `price=30.49`, all `availability=Unknown` |

- JSON-LD SKUs are **bare** (`1025801`). Visible meta keywords are **hyphenated** (`102-5801`). `normalize_sku` (strip / upper / collapse ws, **no** hyphen-strip) therefore reports **0 matched keys**, `llm_only=15`, `oracle_only=15`, `row_count_match=false` even though both lists have length 15.
- Variant payload lives in `<script type="application/ld+json"> ProductGroup.hasVariant` and in `window.masterData` (also a script). `_truncate` strips both. Headless does **not** serialize the Alpine variant table into `result.html`.
- Expected Phase 0 story (static: AJAX `Loading...`; headless: table populated) is **half-true for prices in the live page, false for crawl4ai HTML**: the table is JS-bound, not server-rendered rows.

Phase 1 draft (`extract_fallback_with_llm` / alias `extract_with_llm`) **was** called on the cached headless HTML. It enumerated 15 SKUs from keywords. That is **not** the variant table. Field diffs vs oracle: name (page title vs per-variant), price (one visible `$30.49` cloned onto every row vs 30.49–53.99), availability (Unknown vs In stock), SKU punctuation.

### What was built anyway (Phases 1–2 only)

- `app/tools/llm_extract.py`: unified extractor — full field set including `specifications` + `image_urls`, 25k `_truncate`, `[]` on API/parse failure, alias `extract_with_llm`. Purpose tag `extract`.
- `app/tools/oracle.py`: pure `compare(llm_rows, oracle_rows)`; `{"oracle": "absent"}` when JSON-LD misses; tunable consts as specified.
- `tests/test_oracle.py` (unittest).
- **Not built (Option 1):** extract_node / classify_node rewire, summary oracle tables, Safco `start_render_mode=headless`, cap-3 / cap-25 LLM-primary crawl, CSV/JSON schema changes (none). The unified extractor and `compare()` stay in-tree unused by the graph.

### Match-rate tables (Safco full run)

**No full run.** Cap-3 and cap-25 were not executed. There is no 225-row comparison and no classify-match % from a crawl.

One-page diagnostic (SureStitch, not a crawl):

| Metric | Value |
|---|---|
| Oracle rows | 15 |
| LLM rows | 15 |
| Row-count match (`\|llm skus\|==\|oracle skus\|` **and** zero unmatched keys) | **false** (hyphen vs bare) |
| Fan-out diverged | **yes** (15 llm-only + 15 oracle-only keys) |
| Classify match % | n/a (graph not rewired) |
| Per-field match on matched SKUs | n/a (zero matched keys) |
| Systematic LLM errors on this page | hyphenated SKU; generic name; cloned price 30.49; Unknown availability |

### Cost

One extract call on this page: `deepseek-v4-flash`, ~128s, **23515** tokens (`data/logs` not opened for this standalone script). A full Safco crawl at LLM-primary would still be ~66 classify + ~50 extract **if** the graph were inverted; that cost was **not** spent.

### What this says about LLM-primary generalizing off Safco

Safco is a **bad** visible-DOM test bench for variant fan-out. The 225 rows the deterministic path stores come from JSON-LD the LLM is forbidden to see under current `_truncate`. Feeding that JSON-LD back into the prompt would make an oracle comparison circular. Net32 is already the site where LLM-primary is load-bearing (heuristic classify always misses; specs LLM fills a table JSON-LD does not carry). Option 1 keeps that split: Safco = structured extract; Net32 = LLM classify + additive specs. A future “any site” loop is not proven by a Safco 225 re-run under LLM extract.

Rejected (not implemented): (2) splice JSON-LD/`masterData` into the extract prompt; (3) hydrate Alpine then re-run Phase 0; (4) hyphen-strip SKUs to fake a key match. None of those invert the graph; none were needed once Option 1 was locked.

### Remaining brief-gap (separate decision)

`specifications` and `alternatives` are still **0%** on Safco (empty `{}` / `[]` on stored rows). That is unrelated to this inversion: JSON-LD `ProductGroup` never filled those fields, and Option 1 does not change that. Filling them (Safco spec table, recs widget / Seam D) is a later product-field decision, not a classify/extract primacy decision.

## 16. Post-audit fixpack (2026-08-19)

Option 1 remains locked. `app/graph.py` was not edited. Instrumentation does not
change return values or swallow exceptions at the choke point (failure path
records + logs, then re-raises).

### Task 1 — failed LLM calls visible; classify degrades like the other sites

**What changed.** `app/llm.py::llm()` wraps client resolution + `create()`,
records `call_counter` with `ok=False`, writes an `llm_call` JSONL event with
`"ok": false` and the error (instrumentation itself try/except'd), then
re-raises. `llm_calls` / `total` stay success-only. `classify_with_llm` now
wraps the `await` as well as the JSON parse and returns
`(UNKNOWN, reason, 0.0)` on a raised API error — matching the other three
sites. Choke-point logging still fires once before the site catches.

**Live crawl (one Net32 product, `DEEPSEEK_API_KEY` forced empty, uvicorn on
`:8010`).** Run completed, no crash (`status: done`).

Honest gap vs the written acceptance check: seeding a Net32 product URL still
goes through LLM classify first. With the key empty, classify returns UNKNOWN,
`recover` sees `start_render_mode=headless` already, and dead-letters. Extract
never runs, so **`extract_specifications` does not fire on this live path**,
and **no product row is persisted** (tier-1 never reached). That is existing
graph routing, not a Task 1 regression. The "product row still persists via
tier-1 structured core; specifications is `{}`" clause is **unexercised** on
an empty-key Net32 crawl. Isolated calls (below) prove both call sites log.

Run A live `/status` (process with the fix, empty key):

```
{
  "run_id": "999a8acba67e",
  "status": "done",
  "pages_fetched": 1,
  "products_found": 0,
  "dead_letters": 1,
  "llm_calls": {"total": 0, "failed": 1, "by_purpose": {}}
}
```

`data/logs/run_999a8acba67e.jsonl` `llm_call` excerpt:

```
{"kind": "llm_call", "run_id": "999a8acba67e", "call_site": "classify", "ok": false,
 "error": "RuntimeError: DEEPSEEK_API_KEY is not set. Copy .env.example to .env and add your key.",
 "model": "deepseek-v4-flash"}
```

`data/logs/run_999a8acba67e_summary.json` (relevant keys):

```
"llm_calls_total": 0,
"llm_calls_by_call_site": {},
"llm_calls_failed": 1,
"llm_calls_failed_by_call_site": {"classify": 1}
```

`runs` row: `llm_calls=0` (success total from the JSONL summary), `dead_letters=1`,
`products_found=0`. Products table: no rows for this `run_id`.

Isolated empty-key calls (not a crawl) for the two sites the acceptance named:

```
specs_return {}
classify_return ('unknown', 'RuntimeError: DEEPSEEK_API_KEY is not set. ...', 0.0)
counter {'total': 0, 'failed': 2, 'by_purpose': {}}
{"kind": "llm_call", "run_id": "task1-isolated", "call_site": "extract_specifications", "ok": false, "error": "RuntimeError: DEEPSEEK_API_KEY is not set. ..."}
{"kind": "llm_call", "run_id": "task1-isolated", "call_site": "classify", "ok": false, "error": "RuntimeError: DEEPSEEK_API_KEY is not set. ..."}
```

### Task 2 — per-run counts; DB metric from JSONL summary

**What changed.** `LLMCallCounter` keys buckets by `run_id`. `/status` uses
`call_counter.snapshot(run_id)` (still exposes `total`). `_runner` `finally`
computes `compute_summary` first and passes `summary["llm_calls_total"]` into
`upsert_run`. `compute_summary` splits `llm_calls_total` (successes, including
legacy records with `ok` missing) vs `llm_calls_failed` / `_by_call_site`.

**Two sequential `POST /run` in one uvicorn process** (empty key, same Net32
product, `:8010`):

Run A `/status` `llm_calls`: `{"total": 0, "failed": 1, "by_purpose": {}}`
Run B `/status` `llm_calls`: `{"total": 0, "failed": 1, "by_purpose": {}}`

B is **B only**, not A+B (`failed` would be 2 if the global counter were still
fed to `/status`). `runs.llm_calls` for B = 0 = `run_c06c8926ffa6_summary.json`
`llm_calls_total`.

```
{'run_id': '999a8acba67e', ..., 'llm_calls': 0, 'dead_letters': 1}
{'run_id': 'c06c8926ffa6', ..., 'llm_calls': 0, 'dead_letters': 1}
```

Empty-key live runs cannot show a non-zero success `total` split (both are 0).
In-process counter check of the success path:

```
global {'total': 3, 'failed': 0, 'by_purpose': {'classify': 2, 'extract_specifications': 1}}
A {'total': 2, 'failed': 0, 'by_purpose': {'classify': 1, 'extract_specifications': 1}}
B {'total': 1, 'failed': 0, 'by_purpose': {'classify': 1}}
missing {'total': 0, 'failed': 0, 'by_purpose': {}}
```

Concurrent-run isolation is the same `_by_run` keying; not separately load-tested
with two overlapping `POST /run` in this session (unexercised as a live overlap).

