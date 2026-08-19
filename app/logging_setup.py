"""Additive-only structured logging for the agent's LLM calls and node visits.

Exists to prove the agentic layer is doing real work with artifacts (a JSONL
event log + a computed summary), not claims. Nothing here changes control
flow, exception behavior, or return values anywhere else in the app — every
call site that uses this module only observes and returns void.

`current_run_id`/`current_job_url` are contextvars, not threaded function
parameters: `asyncio.to_thread` (used by `app/tools/llm_extract.py::_llm_async`
to run the sync OpenAI SDK call off the event loop) explicitly copies the
calling context into the worker thread, so a value set here by a node before
it awaits an LLM call is visible at the LLM choke point (`app/llm.py::llm()`)
without adding a `job_url`/`run_id` parameter to any of the four LLM-backed
functions or their call sites.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import TextIO

LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"

current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_run_id", default="")
current_job_url: contextvars.ContextVar[str] = contextvars.ContextVar("current_job_url", default="")

console = logging.getLogger("agent")
if not console.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    console.addHandler(_h)
    console.setLevel(logging.INFO)
    console.propagate = False

_open_files: dict[str, TextIO] = {}


def start_run_logging(run_id: str) -> Path:
    """Open this run's JSONL sink and activate it for log_event(). Call once,
    before the graph starts running."""
    current_run_id.set(run_id)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"run_{run_id}.jsonl"
    _open_files[run_id] = path.open("a", encoding="utf-8")
    console.info(f"[run_start] run_id={run_id} log={path}")
    return path


def stop_run_logging(run_id: str) -> None:
    f = _open_files.pop(run_id, None)
    if f:
        f.close()


def log_event(kind: str, summary: str, **fields) -> None:
    run_id = current_run_id.get()
    record = {"ts": time.time(), "run_id": run_id, "kind": kind, **fields}
    f = _open_files.get(run_id)
    if f:
        try:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
        except Exception:
            pass  # instrumentation must never break a real run
    console.info(f"[{kind}] {summary}")


def log_node_entry(node: str, job) -> None:
    """One record per node entry (Phase 2b). For next_job specifically, this
    is called *after* the pop (there is no job to describe before that point)
    — documented here rather than silently redefined as "entry"."""
    job_url = getattr(job, "url", "") or ""
    job_type = getattr(getattr(job, "type", None), "value", None)
    render_mode = getattr(getattr(job, "render_mode", None), "value", None)
    current_job_url.set(job_url)
    log_event(
        "node_visit",
        f"{node} url={job_url}",
        node=node, job_url=job_url, job_type=job_type, render_mode=render_mode,
    )


def compute_summary(run_id: str) -> dict:
    """Re-read this run's own JSONL file and aggregate — the summary is
    derived from the artifact just written, not from any in-memory counter,
    so it's independently reproducible from the file alone."""
    path = LOG_DIR / f"run_{run_id}.jsonl"
    node_visits: Counter = Counter()
    llm_calls: Counter = Counter()
    llm_calls_failed: Counter = Counter()
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    tokens_present = False

    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("kind") == "node_visit":
                node_visits[rec["node"]] += 1
            elif rec.get("kind") == "llm_call":
                site = rec.get("call_site", "unknown")
                if rec.get("ok") is False:
                    llm_calls_failed[site] += 1
                else:
                    # Missing `ok` (pre-fixpack success records) still counts.
                    llm_calls[site] += 1
                    usage = rec.get("usage")
                    if usage:
                        tokens_present = True
                        for k in tokens:
                            tokens[k] += usage.get(k) or 0

    iterations = node_visits.get("next_job", 0)
    pages_fetched = node_visits.get("fetch", 0)
    llm_calls_total = sum(llm_calls.values())
    llm_calls_failed_total = sum(llm_calls_failed.values())

    summary = {
        "run_id": run_id,
        "node_visits_total": sum(node_visits.values()),
        "node_visits_by_name": dict(node_visits),
        "iterations": iterations,
        "llm_calls_total": llm_calls_total,
        "llm_calls_by_call_site": dict(llm_calls),
        "llm_calls_failed": llm_calls_failed_total,
        "llm_calls_failed_by_call_site": dict(llm_calls_failed),
        "tokens": tokens if tokens_present else None,
        "tokens_note": None if tokens_present else "no llm_call record carried non-null usage",
        "pages_fetched": pages_fetched,
        "llm_calls_per_page": round(llm_calls_total / pages_fetched, 3) if pages_fetched else None,
    }
    out_path = LOG_DIR / f"run_{run_id}_summary.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return summary
