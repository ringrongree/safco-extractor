"""DeepSeek client wrapper (OpenAI-compatible SDK). BUILD_SPEC.md §5.

Only three call sites are allowed to reach this module: classify (on ambiguity),
extract-fallback (structured + selector both failed), and selector-repair.
Never called from fetch, dedup, or storage.
"""
import os
import threading
import time

from openai import OpenAI

from app.logging_setup import current_job_url, current_run_id, log_event

MODEL_DEFAULT = "deepseek-v4-flash"
MODEL_REASONING = "deepseek-v4-pro"

_client: OpenAI | None = None
_lock = threading.Lock()


class LLMCallCounter:
    """Process-wide counter so the UI/BUILD_REPORT can report LLM usage.

    `total` / `by_purpose` count successful responses only (keeps historical
    sample numbers comparable). Failed attempts increment `failed` only —
    they are never folded into `total`. Per-run buckets keep concurrent /
    sequential runs in one process from contaminating each other.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total = 0            # successful calls, global (back-compat)
        self.failed = 0           # failed attempts, global
        self.by_purpose: dict[str, int] = {}
        self._by_run: dict[str, dict] = {}

    def record(self, purpose: str, run_id: str = "", ok: bool = True) -> None:
        with self._lock:
            b = self._by_run.setdefault(run_id, {"total": 0, "failed": 0, "by_purpose": {}})
            if ok:
                self.total += 1
                self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1
                b["total"] += 1
                b["by_purpose"][purpose] = b["by_purpose"].get(purpose, 0) + 1
            else:
                self.failed += 1
                b["failed"] += 1

    def snapshot(self, run_id: str | None = None) -> dict:
        with self._lock:
            if run_id is None:
                return {
                    "total": self.total,
                    "failed": self.failed,
                    "by_purpose": dict(self.by_purpose),
                }
            b = self._by_run.get(run_id, {"total": 0, "failed": 0, "by_purpose": {}})
            return {
                "total": b["total"],
                "failed": b["failed"],
                "by_purpose": dict(b["by_purpose"]),
            }


call_counter = LLMCallCounter()


def _get_client() -> OpenAI:
    global _client
    with _lock:
        if _client is None:
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "DEEPSEEK_API_KEY is not set. Copy .env.example to .env and add your key."
                )
            _client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        return _client


def llm(messages: list[dict], purpose: str, model: str = MODEL_DEFAULT, **kw) -> str:
    """Call DeepSeek. `purpose` is a free-text tag (e.g. "classify", "extract",
    "selector_repair") used only for the call counter, never for routing.

    Control flow is unchanged: a failed client/create still propagates to the
    caller. The except path only records + logs, then re-raises the same
    exception. Success-path logging is still defensively wrapped so a bug in
    instrumentation can never affect this function's real return value.
    """
    run_id = current_run_id.get()
    start = time.perf_counter()
    try:
        client = _get_client()
        resp = client.chat.completions.create(model=model, messages=messages, **kw)
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        call_counter.record(purpose, run_id=run_id, ok=False)
        try:
            log_event(
                "llm_call",
                f"{purpose} model={model} FAILED {type(exc).__name__} latency={latency_ms:.0f}ms",
                call_site=purpose,
                model=model,
                job_url=current_job_url.get(),
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=round(latency_ms, 1),
                ok=False,
            )
        except Exception:
            pass
        raise
    latency_ms = (time.perf_counter() - start) * 1000
    call_counter.record(purpose, run_id=run_id, ok=True)

    try:
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
        response_text = resp.choices[0].message.content if resp.choices else None
        prompt_text = messages[-1]["content"] if messages else ""
        log_event(
            "llm_call",
            f"{purpose} model={model} latency={latency_ms:.0f}ms "
            f"tokens={usage.get('total_tokens') if usage else 'null'}",
            call_site=purpose,
            model=model,
            job_url=current_job_url.get(),
            prompt_chars=len(prompt_text),
            prompt_preview=prompt_text[:500],
            response=response_text,
            usage=usage,
            latency_ms=round(latency_ms, 1),
            ok=True,
        )
    except Exception:
        pass  # instrumentation must never affect a real call's outcome

    return resp.choices[0].message.content or ""
