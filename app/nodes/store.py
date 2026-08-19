"""STORE node — act: persist. Upsert on canonical URL + SKU. BUILD_SPEC.md §10.

Body is wrapped in try/except so a single malformed row (e.g. a SQLite
constraint error) dead-letters that job and lets the run continue, rather than
aborting the whole crawl — closes INVENTORY_REPORT.md finding B / kickoff §4.6.
"""
from __future__ import annotations

import sqlite3

from app.logging_setup import log_node_entry
from app.memory import RunMemory
from app.schemas import ExtractionMethod, FailureRecord, LoopTrace, RecoveryStage
from app.state import GraphState
from app.storage import insert_failure, upsert_product


def make_store_node(memory: RunMemory, conn: sqlite3.Connection):
    async def store_node(state: GraphState) -> dict:
        job = state["job"]
        products = state.get("extracted_products", [])
        assert job is not None
        log_node_entry("store", job)

        try:
            for product in products:
                memory.add_product(product)
                upsert_product(conn, product, memory.run_id)

            memory.log_trace(
                LoopTrace(
                    job_id=job.job_id,
                    url=job.url,
                    decision=f"stored {len(products)} row(s)",
                    extraction_method=ExtractionMethod.NONE,
                ),
                conn,
            )
        except Exception as exc:
            record = FailureRecord(
                job_id=job.job_id,
                url=job.url,
                stage=RecoveryStage.STORE,
                error=f"{type(exc).__name__}: {exc}",
                attempts=job.attempts,
                last_render_mode=job.render_mode,
            )
            memory.dead_letters.append(record)
            insert_failure(conn, record)
        return {}

    return store_node
