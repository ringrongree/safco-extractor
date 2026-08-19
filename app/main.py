"""FastAPI + Jinja2 UI. One page, server-rendered, background crawl task.
BUILD_SPEC.md §11."""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    # uvicorn's default asyncio loop on Windows is Selector-based, which
    # cannot spawn subprocesses -- Playwright (via crawl4ai) needs one to
    # launch the browser. Hit live during UI testing 2026-08-18 as
    # `NotImplementedError` from `asyncio.create_subprocess_exec`; standalone
    # scripts using `asyncio.run()` didn't hit this since Python's default
    # policy there is already Proactor. See BUILD_REPORT.md.
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — avoids adding python-dotenv, which isn't in the
    locked dependency list (BUILD_SPEC.md §4)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from app.graph import run_crawl
from app.llm import call_counter
from app.logging_setup import compute_summary, start_run_logging, stop_run_logging
from app.memory import RunMemory
from app.schemas import Job, JobType
from app.storage import export_csv, export_json, get_connection, upsert_run
from config.registry import resolve_adapter_for_urls
from config.safco import SAFCO_ADAPTER

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
EXPORT_DIR = BASE_DIR / "data" / "exports"

app = FastAPI(title="Safco Catalog Extractor (POC)")

RUNS: dict[str, RunMemory] = {}
TASKS: dict[str, asyncio.Task] = {}


class RunRequest(BaseModel):
    categories: list[str] = SAFCO_ADAPTER.categories
    max_products_per_category: int = SAFCO_ADAPTER.max_products_per_category
    request_delay_seconds: float = SAFCO_ADAPTER.rate_limit


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "categories": SAFCO_ADAPTER.categories,
            "max_products_per_category": SAFCO_ADAPTER.max_products_per_category,
            "request_delay_seconds": SAFCO_ADAPTER.rate_limit,
        },
    )


@app.post("/run")
async def start_run(req: RunRequest):
    run_id = uuid.uuid4().hex[:12]

    try:
        base_adapter = resolve_adapter_for_urls(req.categories)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    adapter = copy.deepcopy(base_adapter)
    adapter.categories = req.categories
    adapter.max_products_per_category = req.max_products_per_category
    adapter.rate_limit = req.request_delay_seconds

    memory = RunMemory(adapter=adapter, run_id=run_id)
    for url in req.categories:
        memory.push(Job(url=url, type=JobType.CATEGORY, render_mode=adapter.start_render_mode))
    memory.status = "running"
    RUNS[run_id] = memory

    async def _runner():
        conn = get_connection()
        started_at = time.time()
        started_iso = _now_iso()
        start_run_logging(run_id)
        try:
            await run_crawl(memory, adapter, conn)
            memory.status = "done"
        except Exception as exc:  # keep the POC alive; report the error via /status
            memory.status = "error"
            memory.error_message = str(exc)
        finally:
            summary = compute_summary(run_id)
            upsert_run(
                conn, run_id, adapter.site_id, memory.status,
                memory.pages_fetched, memory.products_found,
                summary["llm_calls_total"], len(memory.dead_letters),
                time.time() - started_at, started_iso, _now_iso(),
            )
            stop_run_logging(run_id)
            conn.close()

    TASKS[run_id] = asyncio.create_task(_runner())
    return {"run_id": run_id}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(memory: RunMemory) -> dict:
    snap = memory.snapshot()
    snap["done"] = memory.status in ("done", "error")
    return snap


@app.get("/status/{run_id}")
async def status(run_id: str):
    memory = RUNS.get(run_id)
    if memory is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    snap = _snapshot(memory)
    snap["llm_calls"] = call_counter.snapshot(run_id)
    return snap


@app.get("/results/{run_id}", response_class=HTMLResponse)
async def results(request: Request, run_id: str):
    memory = RUNS.get(run_id)
    if memory is None:
        return HTMLResponse("Unknown run_id", status_code=404)
    return TEMPLATES.TemplateResponse(
        request,
        "results.html",
        {"run_id": run_id, "products": memory.products, "snapshot": _snapshot(memory)},
    )


@app.get("/export/{run_id}.csv")
async def export_csv_route(run_id: str):
    memory = RUNS.get(run_id)
    if memory is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    site_id = memory.adapter.site_id or "export"
    path = export_csv(memory.products, EXPORT_DIR / f"{run_id}.csv")
    return FileResponse(path, filename=f"{site_id}_{run_id}.csv", media_type="text/csv")


@app.get("/export/{run_id}.json")
async def export_json_route(run_id: str):
    memory = RUNS.get(run_id)
    if memory is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    site_id = memory.adapter.site_id or "export"
    path = export_json(memory.products, EXPORT_DIR / f"{run_id}.json")
    return FileResponse(path, filename=f"{site_id}_{run_id}.json", media_type="application/json")
