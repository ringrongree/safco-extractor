"""DeepSeek client wrapper (OpenAI-compatible SDK). BUILD_SPEC.md §5.

Only three call sites are allowed to reach this module: classify (on ambiguity),
extract-fallback (structured + selector both failed), and selector-repair.
Never called from fetch, dedup, or storage.
"""
import os
import threading

from openai import OpenAI

MODEL_DEFAULT = "deepseek-v4-flash"
MODEL_REASONING = "deepseek-v4-pro"

_client: OpenAI | None = None
_lock = threading.Lock()


class LLMCallCounter:
    """Process-wide counter so the UI/BUILD_REPORT can report LLM usage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total = 0
        self.by_purpose: dict[str, int] = {}

    def record(self, purpose: str) -> None:
        with self._lock:
            self.total += 1
            self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"total": self.total, "by_purpose": dict(self.by_purpose)}


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
    "selector_repair") used only for the call counter, never for routing."""
    client = _get_client()
    resp = client.chat.completions.create(model=model, messages=messages, **kw)
    call_counter.record(purpose)
    return resp.choices[0].message.content or ""
