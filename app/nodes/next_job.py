"""NEXT_JOB — the "back to queue" loop driver from BUILD_SPEC.md §6's diagram.
Pops the frontier and resets per-iteration state; routes to END when the
frontier is empty or the page budget is exhausted.

Wrapped in try/except: this node has no incoming conditional edge to route a
failure to RECOVER (nothing feeds it a stage_failed to dispatch on), so the
fail-safe here is to dead-letter what we know and end the run cleanly instead
of crashing — a corrupted frontier isn't safe to keep popping from. Closes
INVENTORY_REPORT.md finding B / kickoff §4.6 for this node."""
from __future__ import annotations

import sqlite3

from app.logging_setup import log_event, log_node_entry
from app.memory import RunMemory
from app.schemas import FailureRecord, RecoveryStage
from app.state import GraphState
from app.storage import insert_failure


def make_next_job_node(memory: RunMemory, conn: sqlite3.Connection):
    async def next_job_node(state: GraphState) -> dict:
        try:
            if memory.budget_exhausted():
                log_event("node_visit", "next_job budget_exhausted -> done",
                          node="next_job", job_url=None, job_type=None, render_mode=None)
                return {"done": True}

            job = memory.pop()
            if job is None:
                log_event("node_visit", "next_job frontier_empty -> done",
                          node="next_job", job_url=None, job_type=None, render_mode=None)
                return {"done": True}

            # Logged after the pop, not before — there is no job to describe
            # until this point (documented in log_node_entry's docstring).
            log_node_entry("next_job", job)

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
        except Exception as exc:
            record = FailureRecord(
                job_id="frontier",
                url="",
                stage=RecoveryStage.NEXT_JOB,
                error=f"{type(exc).__name__}: {exc}",
                attempts=0,
                last_render_mode=None,
            )
            memory.dead_letters.append(record)
            insert_failure(conn, record)
            return {"done": True}

    return next_job_node
