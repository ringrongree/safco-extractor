# Safco Catalog Extractor (POC)

## 1. What this is

Working two-site catalog POC: Safco is JSON-LD/static with a real 225-row dump; Net32 is headless + LLM for page type and spec tables. It is **not** a demonstrated 3-tier extract cascade, and the LLM is **not** a proven variant extractor.

---

## 2. Architecture overview

The crawl is a LangGraph state machine: observe → analyze → act, recursed until the frontier is empty or the page budget is hit. Shared `RunMemory` holds the frontier, visited set, governor counters, and in-process product list. The graph’s own cycle **is** the crawl — there is no outer `while` over URLs.

**Eight nodes:** `next_job`, `fetch`, `classify`, `enqueue`, `extract`, `validate`, `store`, `recover`.

Edge flow: `next_job → fetch → classify → enqueue|extract → validate → store → next_job`. `recover` is a branch off typed failures (`fetch_error` / unknown page type / extract or validate `stage_failed`); it routes back to `fetch`, `extract`, or `next_job` (dead-letter).

```
  next_job ──empty/budget──► END
      │
      ▼
    fetch ──fetch_error──► recover ──retry/escalate──► fetch
      │                         │
      ▼                         ├──repair_selector──► extract
   classify                     └──dead_letter──► next_job
      │
      ├── category|listing ──► enqueue ──► next_job
      ├── product ───────────► extract ──fail──► recover
      └── unknown ───────────► recover            │
                                     extract ok ──▼
                                               validate ──fail──► recover
                                                   │ ok
                                                 store ──► next_job
```

---

## 3. Why this approach

**Loop-as-graph.** A cyclic crawl with recovery as an edge is LangGraph’s native shape: nodes do work, conditional edges route, state is explicit. An outer Python loop calling the graph once per URL would hide that machine.

**Intelligence in the graph, mechanics in tools.** Nodes decide (page type, cascade tier, recover vs dead-letter). Fetch, parse, JSON-LD, upsert, and LLM HTTP live in `app/tools/` (plus `app/llm.py` / `app/storage.py`) so they can be tested without spinning the graph.

**LLM only on interpretation.** Allowed call sites: classify, spec-table fill, cascade last-resort extract, selector-repair. Not fetch, not dedup, not store. Cost discipline: stop the extract cascade at first success. In this POC, classify + spec-fill **work** on Net32; last-resort extract and selector-repair are **wired but never fired in a crawl**.

**Disclosed deviation from the brief’s render split.** The take-home assumed AJAX-only price/grid and headless-first. Safco **server-renders JSON-LD** (`ProductGroup` / `ItemList`) in static HTML, so the adapter is **static-first**, with headless as `recover` escalate-render. Net32 is the opposite: Cloudflare 403 on static, so the adapter is **headless-first**.

---

## 4. Agent responsibilities

Brief roles (discovery / navigator / classifier / extractor / validator-dedup / recovery) map onto nodes:

| Node | Brief role | Responsibility | Tools |
|---|---|---|---|
| `next_job` | navigator (frontier) | Pop next `Job`; stop on empty queue or `max_pages` | `RunMemory.pop`, `budget_exhausted` |
| `fetch` | observe | Static (`httpx`) or headless (`crawl4ai` / Playwright) per `job.render_mode` | `fetch_static`, `HeadlessFetcher.fetch` |
| `classify` | classifier | Page type: heuristic first; DeepSeek if ambiguous | `classify_heuristic`, `classify_with_llm` |
| `enqueue` | discovery + navigator | Enqueue subcats + product URLs; pagination helpers; respect per-category cap | `structured_extract_subcategory_urls`, `structured_extract_listing_urls`, selector discovery fallbacks, `normalize_url` |
| `extract` | extractor | Cascade: JSON-LD → config selectors → LLM last resort; optional LLM spec overlay | `structured_extract_product`, CSS helpers, `extract_specifications_with_llm`, `extract_fallback_with_llm` |
| `validate` | validator | Drop rows with neither name nor SKU | in-node gate (Pydantic already shapes the row) |
| `store` | validator-dedup | Idempotent upsert; in-memory list for the UI | `upsert_product`, `RunMemory.add_product` |
| `recover` | recovery | retry / escalate-render / repair-selector / dead-letter | `repair_selector_with_llm`, `insert_failure` |

---

## 5. Setup & execution

Cold clone (from this directory):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
crawl4ai-setup
cp .env.example .env # add DEEPSEEK_API_KEY
uvicorn app.main:app --reload

open http://localhost:8000
```

UI routes:

| Method | Path |
|---|---|
| `GET` | `/` |
| `POST` | `/run` |
| `GET` | `/status/{id}` |
| `GET` | `/results/{id}` |
| `GET` | `/export/{id}.csv` |
| `GET` | `/export/{id}.json` |

**Caveat:** `/status`, `/results`, and `/export` read an in-process `RUNS` dict keyed by `run_id`. They only work in the **same process** that handled `POST /run`. After a restart they 404 even if SQLite still has products/traces.

POC caps live on `SiteAdapter` (`config/safco.py`, `config/net32.py`): Safco `max_products_per_category=25`, `max_pages=200`, `rate_limit=2.0`; Net32 `12` / `20` / `6.0`. Host routing is `config/registry.py`.

---

## 6. Sample output schema

Five Pydantic models in `app/schemas.py`. Missing fields are explicit `null` / empty collections — never fabricated.

Committed dumps: `data/samples/` (`safco_all_products.{json,csv}`, per-category Safco files, `net32/net32_gloves.{json,csv}`). SQLite at `data/safco.db` is gitignored and is **not** the deliverable dataset.

### Product (one row per variant)

| Field | Type | Notes |
|---|---|---|
| `product_id` | `str` | UUID hex |
| `variant_id` | `str \| null` | SKU when present |
| `name`, `brand`, `sku` | `str \| null` | |
| `category_path` | `list[str]` | Seed category path from the job, not the full breadcrumb root |
| `url` | `str \| null` | **Canonical** URL — half of the upsert key |
| `crawl_url` | `str \| null` | URL actually fetched (may differ from canonical) |
| `price` | `float \| null` | |
| `currency` | `str \| null` | |
| `price_status` | enum | `visible` \| `gated` \| `not_found` |
| `pack_size` | `str \| null` | Regex from variant name when it matches |
| `availability` | enum | Schema has six values; **seen in this POC: `In stock` / `Unknown` only** |
| `description` | `str \| null` | |
| `specifications` | `dict[str, str]` | **0%** on Safco sample; **12/12** on Net32 via LLM overlay |
| `image_urls` | `list[str]` | |
| `alternatives` | `list[str]` | **0%** on both samples (recs-widget stub) |
| `extraction_method` | enum | `structured` \| `selector` \| `llm` \| `none`. Safco sample: **225/225 `structured`**. Net32 sample: tagged `llm` because specs (and often manufacturer code → `sku`) were filled **after** JSON-LD succeeded — **not** cascade tier 3 |
| `scraped_at` | `str` | ISO timestamp |
| `source_hash` | `str \| null` | Hash of name/sku/price/availability/description for change detection |

Upsert key: **`(canonical url, sku)`**.

**Safco sample (225 rows):** 109 sutures + 116 gloves; 225/225 structured; **0 LLM calls**; **66 pages**; elapsed **~138–145s** (JSONL spans 138.3s / 144.7s). `pack_size` **69.8%** (157/225). `specifications` **0%**. `alternatives` **0%**.

**Net32 sample (12 rows):** specs **100%** via LLM; `alternatives` **0%**; price **11/12**. Matching log: 13 pages, 25 successful LLM calls (classify + `extract_specifications`), 0 cascade-extract / selector-repair calls.

### LoopTrace

Per-iteration observability: `job_id`, `url`, `page_type`, `reasoning`, `decision`, `confidence`, `render_mode`, `extraction_method`, `timestamp`, `html_blob_path`. **Persisted to SQLite `traces`.** HTML blobs are **not** written (`html_blob_path` stays null). The stored `extraction_method` column is not a reliable cascade-tier signal (nodes often write `none`).

### Job

Frontier unit: `url`, `type` (`category` \| `listing` \| `product`), `category_path`, `parent_url`, `depth`, `attempts`, `render_mode`.

### FailureRecord

Dead-letter: `job_id`, `url`, `stage`, `error`, `attempts`, `last_render_mode`, `timestamp`. Written to SQLite `failures`. Never a silent drop.

### SiteAdapter

Per-site config as data: URLs, caps, `start_render_mode`, pagination, selectors, `fill_missing_specifications_via_llm` (Safco `False` → 0 LLM on the 225-row run; Net32 `True`).

---

## 7. Limitations (current POC)

- Safco `specifications` and `alternatives` are **0%**. No PDF/brochure route; no recs-widget extraction (`AlternativesConfig` is a seam, unused).
- Cascade **tier 2 (selector)** and **tier 3 (LLM product extract)** never fired in a real crawl. Stored Safco data is tier-1 JSON-LD only.
- Isolation LLM product-extract **fails the JSON-LD oracle** (0 SKU matches: hyphenated keyword SKUs vs oracle bare SKUs). Not a working variant extractor.
- Selector-repair is coded; **never exercised**. `deepseek-v4-pro` was **never observed** in logs. Observed model: `deepseek-v4-flash` (OpenAI SDK → `api.deepseek.com`, `DEEPSEEK_API_KEY`).
- Pagination is implemented (`query_param` / `path_suffix`); **never fired** at POC caps.
- `classify_heuristic` is Magento/Safco-shaped (`/catalog/`, `/product/`). Other hosts LLM-classify or fail (unknown → recover → often dead-letter if already headless).
- Availability: only `In stock` / `Unknown` seen. `gated` prices, backordered, etc. have **no real coverage**.
- Uncaught exceptions in `fetch` / `extract` / `validate` **abort the run**. `recover` only sees typed `fetch_error` / `stage_failed`.
- UI results/export are **process-local**, not durable.
- Windows: Proactor policy in `app/main.py`; `uvicorn --reload` + Playwright is a known conflict (drop `--reload` if a run must headless); deep clone paths can hit `MAX_PATH` via crawl4ai/chardet (not re-tested this pass).

---

## 8. Failure handling

Four recovery paths in `app/nodes/recover.py`:

| Path | Behavior | Fired in a real crawl? |
|---|---|---|
| **retry** (w/ backoff) | Same render mode; anti-bot errors sleep 8s; bounded by `MAX_ATTEMPTS` (2 recovery passes) | **Yes** (fetch exhaustion, `attempts=3`) |
| **escalate-render** | static → headless | **Yes** |
| **repair-selector** | DeepSeek (`deepseek-v4-pro`) proposes a CSS selector; cached on `RunMemory` for the run | **No** — coded, never entered |
| **dead-letter** | `FailureRecord` → SQLite + `RunMemory.dead_letters` | **Yes** |

Dead-letter **never silently drops**. The Safco 225-row sample runs had **0** recover visits; other experiments produced failure rows — do not mix those.

---

## 9. How to scale to full-site crawling in production

Mapped from the planning/spec future stack to **triggers**, not a shopping list:

- **Distributed frontier** (Crawlee / Scrapy / Redis, Postgres for the product store) when the in-process deque + SQLite outgrow one process.
- **Firecrawl / proxy pool** when static+headless is not enough anti-bot (Net32 already shows Cloudflare sensitivity; this POC only slows the rate).
- **Temporal** (or equivalent) when you need durable resume across process death — LangGraph checkpoints are not wired; `RunMemory` dies with the process.
- **ScrapeGraphAI** (or similar) when adding suppliers that lack Safco-quality JSON-LD and need multi-site extraction beyond two adapters.
- **Auth session** when prices are actually gated (`price_status=gated` exists; not exercised against a gated account).
- **Config-driven multi-site** stays the extension point: new `SiteAdapter` + registry entry. Classification is **not** fully config-driven today (heuristic still Magento-shaped).

---

## 10. How to monitor data quality

- **Field-fill rate split by `extraction_method`.** A shift from `structured` toward `selector`/`llm` is a site-change alarm. Treat Net32’s `llm` tag as “JSON-LD + specs overlay,” not cascade fallback.
- **LLM-call count and token budget** (JSONL `usage.total_tokens`; UI `/status` exposes call counts). Healthy Safco cap-25: **0** calls. Net32 12-product proof: **25** successful calls.
- **Dead-letter queue depth** (`failures` table / `memory.dead_letters`). Spikes mean block or URL-shape change, not silent loss.
- **`source_hash` on re-crawl** — designed for change detection; identical re-upsert is implied by the PK. A genuine before/after hash-diff on mutated content was **not** artifacted in this POC.
- **LoopTrace is the observability spine** (SQLite `traces`). It is not a full replay: no HTML blobs, and the `extraction_method` column is weak.
- **LangSmith** is the production tracing upgrade (not in this POC).
