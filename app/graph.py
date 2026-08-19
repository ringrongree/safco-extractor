"""LangGraph spine. Nodes do work; conditional edges route; RunMemory carries
shared state across iterations. BUILD_SPEC.md §6.

The graph's own loop *is* the crawl: NEXT_JOB pops the frontier and routes
back to FETCH until the queue is empty or the budget governor stops it —
matching the "back to queue" arrow in the spec's diagram, rather than an outer
Python while-loop calling the graph once per URL.
"""
from __future__ import annotations

import sqlite3

from langgraph.graph import END, StateGraph

from app.memory import RunMemory
from app.nodes.classify import make_classify_node
from app.nodes.enqueue import make_enqueue_node
from app.nodes.extract import make_extract_node
from app.nodes.fetch import make_fetch_node
from app.nodes.next_job import make_next_job_node
from app.nodes.recover import make_recover_node
from app.nodes.store import make_store_node
from app.nodes.validate import make_validate_node
from app.schemas import PageType, SiteAdapter
from app.state import GraphState
from app.tools.fetch_headless import HeadlessFetcher

RECURSION_LIMIT = 20000  # bounds LangGraph super-steps for a whole crawl run


def build_graph(memory: RunMemory, adapter: SiteAdapter, headless_fetcher: HeadlessFetcher, conn: sqlite3.Connection):
    graph = StateGraph(GraphState)

    graph.add_node("next_job", make_next_job_node(memory))
    graph.add_node("fetch", make_fetch_node(memory, headless_fetcher))
    graph.add_node("classify", make_classify_node(memory))
    graph.add_node("enqueue", make_enqueue_node(memory, adapter))
    graph.add_node("extract", make_extract_node(memory, adapter))
    graph.add_node("validate", make_validate_node(memory))
    graph.add_node("store", make_store_node(memory, conn))
    graph.add_node("recover", make_recover_node(memory, adapter, conn))

    graph.set_entry_point("next_job")

    graph.add_conditional_edges(
        "next_job",
        lambda s: "end" if s.get("done") else "go",
        {"end": END, "go": "fetch"},
    )

    graph.add_conditional_edges(
        "fetch",
        lambda s: "recover" if s.get("fetch_error") else "classify",
        {"recover": "recover", "classify": "classify"},
    )

    def route_after_classify(s: GraphState) -> str:
        pt = s.get("page_type")
        if pt in (PageType.CATEGORY, PageType.LISTING):
            return "enqueue"
        if pt == PageType.PRODUCT:
            return "extract"
        return "recover"

    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {"enqueue": "enqueue", "extract": "extract", "recover": "recover"},
    )

    graph.add_edge("enqueue", "next_job")

    graph.add_conditional_edges(
        "extract",
        lambda s: "recover" if s.get("stage_failed") == "extract" else "validate",
        {"recover": "recover", "validate": "validate"},
    )

    graph.add_conditional_edges(
        "validate",
        lambda s: "recover" if s.get("stage_failed") == "validate" else "store",
        {"recover": "recover", "store": "store"},
    )

    graph.add_edge("store", "next_job")

    def route_after_recover(s: GraphState) -> str:
        action = s.get("recovery_action")
        if action in ("retry", "escalate_render"):
            return "fetch"
        if action == "repair_selector":
            return "extract"
        return "next_job"  # dead_letter

    graph.add_conditional_edges(
        "recover",
        route_after_recover,
        {"fetch": "fetch", "extract": "extract", "next_job": "next_job"},
    )

    return graph.compile()


async def run_crawl(memory: RunMemory, adapter: SiteAdapter, conn: sqlite3.Connection) -> None:
    async with HeadlessFetcher() as headless_fetcher:
        compiled = build_graph(memory, adapter, headless_fetcher, conn)
        await compiled.ainvoke({}, config={"recursion_limit": RECURSION_LIMIT})
