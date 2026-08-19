"""RECOVER node — an edge every node can take. BUILD_SPEC.md §13.

retry | escalate render | repair selector | dead-letter. Never silently drops
a failure: anything that exhausts recovery becomes a FailureRecord.
"""
from __future__ import annotations

import sqlite3

from app.memory import RunMemory
from app.schemas import FailureRecord, RecoveryStage, RenderMode, SiteAdapter
from app.state import GraphState
from app.storage import insert_failure
from app.tools.llm_extract import repair_selector_with_llm

MAX_ATTEMPTS = 2


def make_recover_node(memory: RunMemory, adapter: SiteAdapter, conn: sqlite3.Connection):
    async def recover_node(state: GraphState) -> dict:
        job = state["job"]
        stage = state.get("stage_failed") or "unknown"
        html = state.get("html", "")
        assert job is not None

        job.attempts += 1

        def dead_letter(error: str) -> dict:
            record = FailureRecord(
                job_id=job.job_id,
                url=job.url,
                stage=_to_stage(stage),
                error=error,
                attempts=job.attempts,
                last_render_mode=job.render_mode,
            )
            memory.dead_letters.append(record)
            insert_failure(conn, record)
            return {"job": job, "recovery_action": "dead_letter"}

        if job.attempts > MAX_ATTEMPTS:
            return dead_letter(f"exhausted recovery after {job.attempts} attempts at stage '{stage}'")

        if stage == "fetch":
            error = state.get("fetch_error") or "fetch failed"
            if job.render_mode == RenderMode.STATIC:
                job.render_mode = RenderMode.HEADLESS
                return {"job": job, "recovery_action": "escalate_render"}
            return {"job": job, "recovery_action": "retry"}

        if stage == "classify":
            if job.render_mode == RenderMode.STATIC:
                job.render_mode = RenderMode.HEADLESS
                return {"job": job, "recovery_action": "escalate_render"}
            return dead_letter("page type still unknown after headless render")

        if stage == "extract":
            # Structured data + selectors both failed on a static fetch: this
            # might genuinely be an AJAX-only irregular page (what the recon
            # doc assumed was true of *every* product page). Try headless once
            # before spending an LLM call on selector repair.
            if job.render_mode == RenderMode.STATIC:
                job.render_mode = RenderMode.HEADLESS
                return {"job": job, "recovery_action": "escalate_render"}

            field = "product_name"
            new_selector = await repair_selector_with_llm(html, field, adapter.selectors.get(field, "h1"))
            if new_selector:
                memory.repaired_selectors[field] = new_selector
            return {"job": job, "recovery_action": "repair_selector"}

        if stage == "validate":
            return dead_letter("extraction cascade produced no usable name/sku after all tiers")

        return dead_letter(f"unrecognized failure stage '{stage}'")

    return recover_node


def _to_stage(stage: str) -> RecoveryStage:
    try:
        return RecoveryStage(stage)
    except ValueError:
        return RecoveryStage.FETCH
