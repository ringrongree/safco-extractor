"""NEXT_JOB — the "back to queue" loop driver from BUILD_SPEC.md §6's diagram.
Pops the frontier and resets per-iteration state; routes to END when the
frontier is empty or the page budget is exhausted."""
from __future__ import annotations

from app.memory import RunMemory
from app.state import GraphState


def make_next_job_node(memory: RunMemory):
    async def next_job_node(state: GraphState) -> dict:
        if memory.budget_exhausted():
            return {"done": True}

        job = memory.pop()
        if job is None:
            return {"done": True}

        return {
            "job": job,
            "html": "",
            "render_mode_used": None,
            "fetch_error": None,
            "page_type": None,
            "classify_reasoning": "",
            "classify_confidence": 0.0,
            "extracted_products": [],
            "extraction_method": None,
            "validation_ok": False,
            "validation_errors": [],
            "recovery_action": None,
            "stage_failed": None,
            "done": False,
        }

    return next_job_node
