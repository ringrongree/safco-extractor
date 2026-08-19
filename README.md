# Safco Catalog Extractor (POC)

An agent-based scraper that crawls two Safco Dental Supply categories, extracts
every product (and every variant), normalizes the data, and stores it in a
queryable form — built per `BUILD_SPEC.md`, the locked build brief. See
`BUILD_REPORT.md` for an honest log of what's built, what's stubbed, and what
was learned from the live site along the way.

## 1. Architecture overview

A LangGraph state machine is the spine. Nodes do work; conditional edges route;
a `RunMemory` object (frontier queue, visited set, canonical map, governor
counters) carries state across iterations. The graph's own loop *is* the crawl
— a `NEXT_JOB` node pops the frontier and routes back to `FETCH` until the
queue is empty or the budget governor stops it, rather than an outer Python
`while` loop calling the graph once per URL.

```
        ┌──────────┐
        │ NEXT_JOB │◄─────────────────────────────┐
        └────┬─────┘                               │
     done?    │ url                                 │
      END ◄───┤                                     │
             ▼                                      │
        ┌──────────┐        fetch error       ┌───────────┐
        │  FETCH   │ ────────────────────────► │  RECOVER  │
        └────┬─────┘                            └─────┬─────┘
             ▼ html                        retry/escalate/repair │ dead-letter
        ┌──────────┐                                  │          │
        │ CLASSIFY │ ── unknown ──────────────────────┘          │
        └────┬─────┘                                             │
     ┌───────┼─────────┐                                         │
     ▼       ▼         ▼                                         │
 category  listing   product                                     │
     │       │         │                                         │
     └───┬───┘         ▼                                         │
         ▼         ┌──────────┐   extract fails (after retries)  │
    ┌──────────┐    │ EXTRACT  │──────────────────────────────────┤
    │ ENQUEUE  │    └────┬─────┘                                  │
    └────┬─────┘         ▼ ok                                     │
         │           ┌──────────┐  fails                          │
         │           │ VALIDATE │──────────────────────────────────┘
         │           └────┬─────┘
         │                ▼ ok
         │           ┌──────────┐
         │           │  STORE   │
         │           └────┬─────┘
         └────────────────┴──────────► back to NEXT_JOB
```

**Agents decide, tools execute.** Every node is a thin async function bound to
shared `RunMemory`/`SiteAdapter` via closure (`app/nodes/*.py`); the actual
mechanics (HTTP fetch, Playwright render, HTML parsing, JSON-LD extraction,
DeepSeek calls) live in stateless, individually testable functions under
`app/tools/*.py`.

## 2. Why this approach

- **LangGraph over a hand-rolled loop or plain asyncio**: cycles (nodes routing
  back to earlier nodes) are LangGraph's core primitive, which is exactly what
  "keep crawling until the queue's empty, with recovery as an edge every node
  can take" needs. It also reads as a real state machine to a reviewer, not a
  script with `if` statements pretending to be agentic.
- **Extraction cascade (structured data → config selectors → LLM), stop at
  first success**: cost discipline. An LLM call is the most expensive, least
  reliable way to get a field — it's the last resort, not the first move.
- **Canonical URL (+ SKU) as the dedup/storage key, not the crawl path**: the
  live site confirmed crawl URL and canonical URL diverge
  (`/catalog/.../safco-surestitch-sutures` → `/product/safco-surestitch-trade-sutures`).
  Keying on crawl path would silently duplicate rows across re-crawls.
- **Render split as an upfront-ish routing rule, refined by what the live site
  actually does**: see §4 below — this is the one place the build deviated
  from the original recon assumption, based on evidence gathered while
  building, and it's the most important thing to read before trusting the
  rest of this document.

## 3. Agent responsibilities

| Node | Responsibility | Tools it calls |
|---|---|---|
| `FETCH` | Observe: tier-routed fetch (static now, headless only on escalation) | `fetch_static`, `fetch_headless` |
| `CLASSIFY` | Analyze: page type — heuristic first, DeepSeek only on ambiguity | `classify_heuristic`, `classify_with_llm` |
| `ENQUEUE` | Act/navigate: discover sub-categories + product URLs, respect caps, handle pagination | `structured_extract_subcategory_urls`, `structured_extract_listing_urls`, selector fallback |
| `EXTRACT` | Act: run the 3-tier cascade | `structured_extract_product`, selector helpers, `extract_fallback_with_llm` |
| `VALIDATE` | Data-quality gate: drop rows with neither name nor SKU | — (Pydantic already enforces the schema at construction) |
| `STORE` | Act: idempotent upsert | `storage.upsert_product` |
| `RECOVER` | Decide: retry / escalate render / repair selector / dead-letter | `repair_selector_with_llm`, re-routes to `FETCH`/`EXTRACT` |

## 4. What the live site actually does (and why the design changed mid-build)

The take-home brief and the internal planning doc both assumed price and the
product grid are **AJAX-only** and need headless rendering everywhere. That
assumption was tested directly (BUILD_SPEC.md §18 step 1 — see
`BUILD_REPORT.md` for the full proof log) and turned out to be half right:

- The **listing grid on a raw static fetch is genuinely empty** (`Loading...`
  placeholder) — headless *is* needed to see it rendered visually.
- But Magento/Hyva **also server-renders a parallel JSON-LD `ItemList`** with
  the same product URLs, names, SKUs, and prices, present in the plain static
  HTML at every `/catalog/` level (including top-level category rollups that
  visually show only sub-categories). A second `ItemList` (name ending
  "Subcategories", items typed `CollectionPage`) carries the sub-category
  links the same way.
- Product pages carry a `ProductGroup` JSON-LD block with a `hasVariant[]`
  array — full SKU/name/price/currency/availability per variant — also
  present in static HTML.
- Prices were **not gated** for the session used during this build, contrary
  to the recon doc's assumption ("unable to process orders for your account").
  `price_status` still supports `gated`/`not_found` for when that isn't true.

**Net effect**: every job (category, listing, product) now starts at
`render_mode = static`, matching `config/safco.py`'s `start_render_mode`.
Headless is reserved for `RECOVER`'s escalate-render path — the fallback the
spec's own §8 describes ("if a static fetch looks empty/AJAX-gated, retry the
same URL headless") — rather than an upfront blanket rule. In the actual
sample run this fallback essentially never fires, because the JSON-LD is
reliable; it's implemented and was verified independently (see
`BUILD_REPORT.md`), but it isn't exercised by the happy path on this
particular site. This is disclosed, not hidden, because it's a real deviation
from BUILD_SPEC.md §8's letter — justified by evidence gathered during the
build, in the spirit of §0's instruction to read the recon as a starting
point, not gospel.

## 5. Setup & execution

```bash
git clone <this repo> safco-extractor
cd safco-extractor
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
crawl4ai-setup                   # installs Playwright browsers
cp .env.example .env             # add your DEEPSEEK_API_KEY
uvicorn app.main:app --reload
# open http://localhost:8000
```

The index page is pre-filled with the two Safco categories and the POC caps
from `config/safco.py`. Click **Run**; the page polls `/status/{run_id}` for
live counters and links to `/results/{run_id}` for the product table, with
CSV/JSON export links.

**Windows note**: `uvicorn --reload` forces a Selector-based asyncio event
loop on Windows, which can't spawn the subprocess Playwright needs for a
headless fetch. Headless is only used as a `RECOVER` fallback (see §4), so a
normal run against Safco is unaffected — but if you need a run that might
genuinely escalate to headless, drop `--reload`. See `BUILD_REPORT.md` §6–7
for the full story.

To reproduce the committed sample dataset directly (no UI, no server):

```bash
python -c "
import asyncio, copy
from app.memory import RunMemory
from app.schemas import Job, JobType, RenderMode
from app.storage import get_connection
from app.graph import run_crawl
from config.safco import SAFCO_ADAPTER

async def main():
    adapter = copy.deepcopy(SAFCO_ADAPTER)
    memory = RunMemory(adapter=adapter, run_id='sample_run')
    for url in adapter.categories:
        memory.push(Job(url=url, type=JobType.CATEGORY, render_mode=RenderMode.STATIC))
    conn = get_connection()
    await run_crawl(memory, adapter, conn)
    conn.close()
    print(memory.snapshot())

asyncio.run(main())
"
```

## 6. Output schema

One row per **variant** (`app/schemas.py::Product`):

| Field | Type | Notes |
|---|---|---|
| `product_id`, `variant_id` | str | `variant_id` is the SKU |
| `name`, `brand`, `sku` | str \| null | |
| `category_path` | list[str] | breadcrumb, "Home" and the leaf name dropped |
| `url` | str | **canonical** URL — the dedup/storage key |
| `crawl_url` | str | URL actually fetched (may differ from canonical) |
| `price`, `currency` | float \| null, str \| null | |
| `price_status` | enum | `visible` \| `gated` \| `not_found` |
| `pack_size` | str \| null | regex-guessed from the variant name, e.g. `"12/box"` |
| `availability` | enum | `In stock` / `Partially in stock` / `Backordered` / `Special order` / `Out of stock` / `Unknown` |
| `description`, `specifications`, `image_urls`, `alternatives` | | `specifications`/`alternatives` are stubbed empty — see BUILD_REPORT |
| `extraction_method` | enum | `structured` \| `selector` \| `llm` — the data-quality signal |
| `scraped_at`, `source_hash` | | `source_hash` detects change on re-crawl |

Missing fields are explicit `null`, never fabricated. Full contract:
`app/schemas.py` (5 Pydantic models — `LoopTrace`, `Product`, `Job`,
`FailureRecord`, `SiteAdapter`).

Storage: SQLite at `data/safco.db`, upsert on `(canonical url, sku)` — idempotent
re-runs. `data/samples/` has the committed CSV/JSON exports (per-category and
combined) from the sample run — see `BUILD_REPORT.md` for the exact numbers.

## 7. Limitations (out of scope for draft 1, per BUILD_SPEC.md §14)

- **No authenticated pricing session.** `price_status` supports `gated`, but
  the session used for this build saw visible prices via JSON-LD, so that path
  is implemented but untested against a genuinely gated account.
- **Spec detail inside linked brochure PDFs** is not followed or parsed.
- **`specifications` and `alternatives`** are always empty in this draft — no
  reliable structured or selector source was confirmed for them on Safco's
  product pages in the time available. Explicit null/empty, not fabricated.
- **Escalation tiers beyond static/headless** (stealth, proxies, unlocker,
  CAPTCHA) are documented as a scale path (§9 below), not built.
- **No distributed frontier.** SQLite + in-process structures only; a run's
  `RunMemory` lives in the FastAPI process and is lost on restart mid-run.
- **No LangSmith/external observability.** `LoopTrace` + this report is it.
- **Pagination is implemented but unexercised** on the live site — no scraped
  category returned more items than fit in a single `ItemList` block, so the
  page-2+ code path hasn't been seen to fire against real data (it was not
  independently unit-tested either — flagged in BUILD_REPORT as a gap).

## 8. Failure handling

Every node failure routes to `RECOVER` (`app/nodes/recover.py`), which decides:

- **retry** — same render mode, bounded by `MAX_ATTEMPTS` (2 recovery passes,
  ~3 tries total before dead-letter).
- **escalate render** — static → headless, for both fetch failures and
  extraction-cascade failures (in case a specific page genuinely is AJAX-only).
- **repair selector** — DeepSeek (`deepseek-v4-pro`) suggests a replacement
  CSS selector when tier-2 extraction comes back empty even after headless
  escalation; the repair is cached in `RunMemory.repaired_selectors` for the
  rest of the run so later product pages benefit without another LLM call.
- **dead-letter** — recovery exhausted → a `FailureRecord` is written to
  SQLite (`failures` table) and `RunMemory.dead_letters`. Nothing is silently
  dropped. Verified against a live failure case (unresolvable host) during
  the build — see `BUILD_REPORT.md`.

## 9. Scaling to full-site crawling in production

1. **Frontier & state**: swap `RunMemory`'s in-process deque/sets for
   Redis (frontier + visited set) and Postgres (canonical map, product store,
   dead-letters). LangGraph's checkpointer can persist graph state directly to
   Postgres for true resumability across process restarts, not just within
   one `ainvoke`.
2. **Concurrency**: the current graph processes one job per "tick" of the
   `NEXT_JOB` loop — fine for a ~200-page POC, not for a full site. Move to
   N worker processes each running their own graph instance, pulling from a
   shared Redis frontier, with the per-category cap and page budget enforced
   centrally (a Postgres row with `SELECT ... FOR UPDATE`, or a Redis atomic
   counter) instead of in a single process's memory.
3. **Escalation tiers**: today it's static → headless → dead-letter. Add
   stealth headless (already documented in the planning doc), then a proxy
   pool (Bright Data / Oxylabs — residential rotation) for IP-ban resilience,
   then a managed unlocker (Firecrawl) as a last resort before giving up on a
   URL. Each tier only activates if the previous one's fetch looks blocked
   (status code, empty-body heuristic, or a CAPTCHA-page fingerprint).
4. **Scheduler**: cron or Temporal-driven re-crawls per category, using
   `source_hash` to skip unchanged products and only re-validate/re-store
   what changed — turns a full re-crawl into a cheap diff pass most days.
5. **Rate limiting**: today it's a flat `request_delay_seconds` per run.
   Production should be per-host token-bucket rate limiting shared across
   worker processes (Redis-backed), with backoff driven by observed 429/405
   responses (the build hit a real Fastly rate-limit — see BUILD_REPORT.md
   — and fixed it with proper headers, but a production crawler at 100x the
   request volume needs adaptive throttling, not just a fixed delay).
6. **Framework swap-in points already anticipated in the design**: Crawlee or
   Scrapy for the crawl engine itself if the in-graph frontier loop becomes
   the bottleneck; ScrapeGraphAI if/when this extends to suppliers whose sites
   don't cooperate with structured data as well as Safco's does; Scrapling if
   selector drift becomes frequent enough that the LLM repair loop is firing
   often (it currently isn't, on this site).

## 10. Data-quality monitoring

- **`extraction_method` field-fill rate per run** is the primary signal —
  tracked live in the UI (`/status/{run_id}`) and logged in `LoopTrace`. A
  rising share of `selector`/`llm` (vs `structured`) rows over time means the
  site changed and selectors/JSON-LD assumptions need review.
- **`source_hash` diffing** across re-crawls flags products whose data
  actually changed vs. re-scraped identically — useful both for "what changed
  today" reporting and for catching extraction *instability* (a hash that
  flips every re-crawl on an unchanged product is a red flag, not a real
  change).
- **Dead-letter rate per category** (`failures` table) — a spike means either
  the category's URL structure changed or the site started blocking the
  crawler; both are actionable alerts, not silent data loss (the whole point
  of never dropping a failure silently).
- **Null-rate per field**, sampled per run — e.g. if `price` nulls jump from
  ~0% to ~80% between runs, that's the gating behavior the recon doc expected
  showing up for real, and should page someone rather than ship a dataset
  that looks fine but silently lost its most business-relevant field.
- **LLM call count vs. page count** — cost/reliability proxy. A healthy run on
  this site should show near-zero LLM calls (structured data covers it); a
  sustained increase means either the cascade's tier-1/2 broke or the site
  changed layout.
- Production upgrade path: push these as metrics to whatever the team already
  uses (Prometheus/Grafana, or simpler — a daily summary row per category in
  Postgres, alerted on via a scheduled query), rather than building a bespoke
  dashboard for the POC.
