"""STORE node — act: persist. Upsert on canonical URL + SKU. BUILD_SPEC.md §10."""
from __future__ import annotations

import sqlite3

from app.memory import RunMemory
from app.schemas import ExtractionMethod, LoopTrace
from app.state import GraphState
from app.storage import upsert_product


def make_store_node(memory: RunMemory, conn: sqlite3.Connection):
    async def store_node(state: GraphState) -> dict:
        job = state["job"]
        products = state.get("extracted_products", [])
        assert job is not None

        for product in products:
            memory.add_product(product)
            upsert_product(conn, product, memory.run_id)

        memory.traces.append(
            LoopTrace(
                job_id=job.job_id,
                url=job.url,
                decision=f"stored {len(products)} row(s)",
                extraction_method=ExtractionMethod.NONE,
            )
        )
        return {}

    return store_node
