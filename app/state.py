"""LangGraph shared state. Per-iteration data only — run-level shared/mutable
state (frontier, visited, governor) lives on RunMemory and is bound into nodes
via closure, not carried in this dict. BUILD_SPEC.md §6."""
from __future__ import annotations

from typing import Optional, TypedDict

from app.schemas import Job, PageType, Product, RenderMode


class GraphState(TypedDict, total=False):
    job: Optional[Job]
    html: str
    render_mode_used: Optional[RenderMode]
    fetch_error: Optional[str]

    page_type: Optional[PageType]
    classify_reasoning: str
    classify_confidence: float

    extracted_products: list[Product]
    extraction_method: Optional[str]

    validation_ok: bool
    validation_errors: list[str]

    recovery_action: Optional[str]   # retry | escalate_render | repair_selector | dead_letter
    stage_failed: Optional[str]

    done: bool
