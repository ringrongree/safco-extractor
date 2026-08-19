"""VALIDATE node — coerce to schema, drop unusable rows, attach provenance.
BUILD_SPEC.md §6. Pydantic already enforces the schema at construction time;
this node's job is the data-quality gate: a row with neither a name nor a SKU
is not worth storing."""
from __future__ import annotations

from app.memory import RunMemory
from app.schemas import ExtractionMethod, LoopTrace
from app.state import GraphState


def make_validate_node(memory: RunMemory):
    async def validate_node(state: GraphState) -> dict:
        job = state["job"]
        products = state.get("extracted_products", [])
        assert job is not None

        valid = [p for p in products if p.name or p.sku]
        dropped = len(products) - len(valid)

        ok = len(valid) > 0
        memory.traces.append(
            LoopTrace(
                job_id=job.job_id,
                url=job.url,
                decision=f"validate: {len(valid)} kept, {dropped} dropped (no name/sku)",
                extraction_method=ExtractionMethod.NONE,
            )
        )
        return {
            "extracted_products": valid,
            "validation_ok": ok,
            "stage_failed": None if ok else "validate",
        }

    return validate_node
