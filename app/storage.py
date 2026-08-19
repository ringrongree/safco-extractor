"""SQLite upsert + CSV/JSON export. BUILD_SPEC.md §10.

Upsert key = (canonical url, sku) — idempotent re-runs. This table is a global
store across all runs (not partitioned by run_id); the UI's per-run views read
from the in-process RunMemory instead, since that's simpler than reconstructing
"what did run X find" from a table designed for cross-run dedup.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from app.schemas import FailureRecord, LoopTrace, Product

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "safco.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    url TEXT NOT NULL,
    sku TEXT NOT NULL,
    run_id TEXT,
    product_id TEXT,
    variant_id TEXT,
    name TEXT,
    brand TEXT,
    category_path TEXT,
    crawl_url TEXT,
    price REAL,
    currency TEXT,
    price_status TEXT,
    pack_size TEXT,
    availability TEXT,
    description TEXT,
    specifications TEXT,
    image_urls TEXT,
    alternatives TEXT,
    extraction_method TEXT,
    scraped_at TEXT,
    source_hash TEXT,
    PRIMARY KEY (url, sku)
);

CREATE TABLE IF NOT EXISTS failures (
    job_id TEXT,
    url TEXT,
    stage TEXT,
    error TEXT,
    attempts INTEGER,
    last_render_mode TEXT,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    run_id TEXT,
    job_id TEXT,
    url TEXT,
    page_type TEXT,
    reasoning TEXT,
    decision TEXT,
    confidence REAL,
    render_mode TEXT,
    extraction_method TEXT,
    timestamp TEXT,
    html_blob_path TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    site_id TEXT,
    status TEXT,
    pages_fetched INTEGER,
    products_found INTEGER,
    llm_calls INTEGER,
    dead_letters INTEGER,
    elapsed_seconds REAL,
    started_at TEXT,
    finished_at TEXT
);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def upsert_product(conn: sqlite3.Connection, product: Product, run_id: str) -> None:
    product.source_hash = product.compute_source_hash()
    conn.execute(
        """
        INSERT INTO products (
            url, sku, run_id, product_id, variant_id, name, brand, category_path,
            crawl_url, price, currency, price_status, pack_size, availability,
            description, specifications, image_urls, alternatives,
            extraction_method, scraped_at, source_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url, sku) DO UPDATE SET
            run_id=excluded.run_id, product_id=excluded.product_id,
            variant_id=excluded.variant_id, name=excluded.name, brand=excluded.brand,
            category_path=excluded.category_path, crawl_url=excluded.crawl_url,
            price=excluded.price, currency=excluded.currency,
            price_status=excluded.price_status, pack_size=excluded.pack_size,
            availability=excluded.availability, description=excluded.description,
            specifications=excluded.specifications, image_urls=excluded.image_urls,
            alternatives=excluded.alternatives, extraction_method=excluded.extraction_method,
            scraped_at=excluded.scraped_at, source_hash=excluded.source_hash
        """,
        (
            product.url, product.sku or "", run_id, product.product_id, product.variant_id,
            product.name, product.brand, json.dumps(product.category_path),
            product.crawl_url, product.price, product.currency,
            product.price_status.value if hasattr(product.price_status, "value") else product.price_status,
            product.pack_size,
            product.availability.value if hasattr(product.availability, "value") else product.availability,
            product.description, json.dumps(product.specifications),
            json.dumps(product.image_urls), json.dumps(product.alternatives),
            product.extraction_method.value if hasattr(product.extraction_method, "value") else product.extraction_method,
            product.scraped_at, product.source_hash,
        ),
    )
    conn.commit()


def insert_failure(conn: sqlite3.Connection, record: FailureRecord) -> None:
    conn.execute(
        "INSERT INTO failures (job_id, url, stage, error, attempts, last_render_mode, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record.job_id, record.url,
            record.stage.value if hasattr(record.stage, "value") else record.stage,
            record.error, record.attempts,
            record.last_render_mode.value if record.last_render_mode else None,
            record.timestamp,
        ),
    )
    conn.commit()


def insert_trace(conn: sqlite3.Connection, trace: LoopTrace, run_id: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO traces (
            trace_id, run_id, job_id, url, page_type, reasoning, decision,
            confidence, render_mode, extraction_method, timestamp, html_blob_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace.trace_id, run_id, trace.job_id, trace.url,
            trace.page_type.value if trace.page_type else None,
            trace.reasoning, trace.decision, trace.confidence,
            trace.render_mode.value if trace.render_mode else None,
            trace.extraction_method.value if trace.extraction_method else None,
            trace.timestamp, trace.html_blob_path,
        ),
    )
    conn.commit()


def upsert_run(
    conn: sqlite3.Connection,
    run_id: str,
    site_id: str | None,
    status: str,
    pages_fetched: int,
    products_found: int,
    llm_calls: int,
    dead_letters: int,
    elapsed_seconds: float,
    started_at: str,
    finished_at: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (
            run_id, site_id, status, pages_fetched, products_found, llm_calls,
            dead_letters, elapsed_seconds, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            site_id=excluded.site_id, status=excluded.status,
            pages_fetched=excluded.pages_fetched, products_found=excluded.products_found,
            llm_calls=excluded.llm_calls, dead_letters=excluded.dead_letters,
            elapsed_seconds=excluded.elapsed_seconds, finished_at=excluded.finished_at
        """,
        (
            run_id, site_id, status, pages_fetched, products_found, llm_calls,
            dead_letters, elapsed_seconds, started_at, finished_at,
        ),
    )
    conn.commit()


_EXPORT_FIELDS = [
    "product_id", "variant_id", "name", "brand", "sku", "category_path", "url",
    "crawl_url", "price", "currency", "price_status", "pack_size", "availability",
    "description", "specifications", "image_urls", "alternatives",
    "extraction_method", "scraped_at", "source_hash",
]


def _product_row(p: Product) -> dict:
    row = p.model_dump()
    row["category_path"] = "|".join(p.category_path)
    row["specifications"] = json.dumps(p.specifications)
    row["image_urls"] = "|".join(p.image_urls)
    row["alternatives"] = "|".join(p.alternatives)
    row["price_status"] = row["price_status"].value if hasattr(row["price_status"], "value") else row["price_status"]
    row["availability"] = row["availability"].value if hasattr(row["availability"], "value") else row["availability"]
    row["extraction_method"] = row["extraction_method"].value if hasattr(row["extraction_method"], "value") else row["extraction_method"]
    return {k: row.get(k) for k in _EXPORT_FIELDS}


def export_csv(products: list[Product], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_EXPORT_FIELDS)
        writer.writeheader()
        for p in products:
            writer.writerow(_product_row(p))
    return out_path


def export_json(products: list[Product], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump([json.loads(p.model_dump_json()) for p in products], f, indent=2)
    return out_path
