"""CLASSIFY node — analyze: page type. Heuristic first, LLM only on ambiguity.
BUILD_SPEC.md §5, §6."""
from __future__ import annotations

import sqlite3

from app.logging_setup import log_node_entry
from app.memory import RunMemory
from app.schemas import ExtractionMethod, LoopTrace, PageType
from app.state import GraphState
from app.tools.llm_extract import classify_with_llm
from app.tools.parse import classify_heuristic


def make_classify_node(memory: RunMemory, conn: sqlite3.Connection):
    async def classify_node(state: GraphState) -> dict:
        job = state["job"]
        html = state.get("html", "")
        assert job is not None
        log_node_entry("classify", job)

        heuristic_type, reasoning = classify_heuristic(html, job.url)
        if heuristic_type is not None:
            page_type, confidence = heuristic_type, 0.9
        else:
            try:
                page_type, reasoning, confidence = await classify_with_llm(html, job.url)
            except Exception as exc:
                # An LLM API failure (not just an unparseable response — that's
                # already handled inside classify_with_llm) must not propagate
                # and crash the whole run. Route it through the same
                # UNKNOWN-page-type path a genuinely ambiguous page takes.
                page_type = PageType.UNKNOWN
                reasoning = f"classify_with_llm raised {type(exc).__name__}: {exc}"
                confidence = 0.0

        memory.log_trace(
            LoopTrace(
                job_id=job.job_id,
                url=job.url,
                page_type=page_type,
                reasoning=reasoning,
                decision=f"classified as {page_type.value}",
                confidence=confidence,
                render_mode=state.get("render_mode_used"),
                extraction_method=ExtractionMethod.NONE,
            ),
            conn,
        )

        return {
            "page_type": page_type,
            "classify_reasoning": reasoning,
            "classify_confidence": confidence,
            # Graph routes PageType.UNKNOWN to RECOVER (route_after_classify);
            # this is the stage label recover_node's own "classify" branch
            # actually keys on. Previously never set — that branch was dead
            # code, so a real classify failure always fell into the generic
            # "unrecognized failure stage" dead-letter on the first attempt
            # instead of getting the designed escalate-to-headless retry.
            "stage_failed": "classify" if page_type == PageType.UNKNOWN else None,
        }

    return classify_node
