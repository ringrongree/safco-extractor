"""CLASSIFY node — analyze: page type. Heuristic first, LLM only on ambiguity.
BUILD_SPEC.md §5, §6."""
from __future__ import annotations

from app.memory import RunMemory
from app.schemas import ExtractionMethod, LoopTrace, PageType
from app.state import GraphState
from app.tools.llm_extract import classify_with_llm
from app.tools.parse import classify_heuristic


def make_classify_node(memory: RunMemory):
    async def classify_node(state: GraphState) -> dict:
        job = state["job"]
        html = state.get("html", "")
        assert job is not None

        heuristic_type, reasoning = classify_heuristic(html, job.url)
        if heuristic_type is not None:
            page_type, confidence = heuristic_type, 0.9
        else:
            page_type, reasoning, confidence = await classify_with_llm(html, job.url)

        memory.traces.append(
            LoopTrace(
                job_id=job.job_id,
                url=job.url,
                page_type=page_type,
                reasoning=reasoning,
                decision=f"classified as {page_type.value}",
                confidence=confidence,
                render_mode=state.get("render_mode_used"),
                extraction_method=ExtractionMethod.NONE,
            )
        )

        return {
            "page_type": page_type,
            "classify_reasoning": reasoning,
            "classify_confidence": confidence,
        }

    return classify_node
