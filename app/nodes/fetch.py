"""FETCH node — observe. Tier-routed: static vs headless. BUILD_SPEC.md §8."""
from __future__ import annotations

from app.memory import RunMemory
from app.schemas import RenderMode
from app.state import GraphState
from app.tools.fetch_headless import HeadlessFetcher
from app.tools.fetch_static import fetch_static


def make_fetch_node(memory: RunMemory, headless_fetcher: HeadlessFetcher):
    async def fetch_node(state: GraphState) -> dict:
        job = state["job"]
        assert job is not None

        await memory.throttle()

        if job.render_mode == RenderMode.HEADLESS:
            result = await headless_fetcher.fetch(job.url)
        else:
            result = await fetch_static(job.url)

        memory.pages_fetched += 1
        memory.mark_visited(job)

        if not result.success:
            return {
                "html": "",
                "render_mode_used": job.render_mode,
                "fetch_error": result.error or "unknown fetch error",
                "stage_failed": "fetch",
            }

        return {
            "html": result.html,
            "render_mode_used": job.render_mode,
            "fetch_error": None,
            "stage_failed": None,
        }

    return fetch_node
