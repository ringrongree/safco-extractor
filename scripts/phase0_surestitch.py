"""Phase 0 prove-first: Safco SureStitch, 15 variants, static vs headless.

Does NOT rewire the graph. Fetches both ways, grades JSON-LD oracle vs the
visible-DOM slice `_truncate` would send the LLM, then (if headless looks
populated) calls the unified extractor once.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        import os
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")

from app.tools.fetch_headless import HeadlessFetcher
from app.tools.fetch_static import fetch_static
from app.tools.llm_extract import _MAX_HTML_CHARS_EXTRACT, _truncate, extract_with_llm
from app.tools.oracle import compare, normalize_sku
from app.tools.structured_data import structured_extract_product

URL = "https://www.safcodental.com/product/safco-surestitch-trade-sutures"
OUT = ROOT / "data" / "phase0"
EXPECTED_N = 15


def _sku_hits_in_text(text: str, skus: list[str]) -> list[str]:
    found = []
    for sku in skus:
        if sku and sku in text:
            found.append(sku)
    return found


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    print("=== PHASE 0: fetch static ===")
    static = await fetch_static(URL)
    static_path = OUT / "surestitch_static.html"
    static_path.write_text(static.html or "", encoding="utf-8")
    print(f"static success={static.success} status={static.status_code} bytes={len(static.html or '')} -> {static_path}")

    print("=== PHASE 0: fetch headless ===")
    async with HeadlessFetcher() as hf:
        headless = await hf.fetch(URL)
    headless_path = OUT / "surestitch_headless.html"
    headless_path.write_text(headless.html or "", encoding="utf-8")
    print(f"headless success={headless.success} status={headless.status_code} bytes={len(headless.html or '')} -> {headless_path}")

    oracle_static = structured_extract_product(static.html or "", URL)
    oracle_headless = structured_extract_product(headless.html or "", URL)
    n_static = len(oracle_static or [])
    n_headless = len(oracle_headless or [])
    print(f"oracle structured_extract_product: static={n_static} headless={n_headless} (expect {EXPECTED_N})")

    static_skus = [p.sku for p in (oracle_static or []) if p.sku]
    headless_skus = [p.sku for p in (oracle_headless or []) if p.sku]
    print(f"oracle static skus ({len(static_skus)}): {static_skus}")
    print(f"oracle headless skus ({len(headless_skus)}): {headless_skus}")

    slice_static = _truncate(static.html or "", _MAX_HTML_CHARS_EXTRACT)
    slice_headless = _truncate(headless.html or "", _MAX_HTML_CHARS_EXTRACT)
    (OUT / "surestitch_static_truncated.txt").write_text(slice_static, encoding="utf-8")
    (OUT / "surestitch_headless_truncated.txt").write_text(slice_headless, encoding="utf-8")

    hits_s = _sku_hits_in_text(slice_static, static_skus or headless_skus)
    hits_h = _sku_hits_in_text(slice_headless, headless_skus or static_skus)
    loading_s = bool(re.search(r"Loading\.\.\.", slice_static, re.I))
    loading_h = bool(re.search(r"Loading\.\.\.", slice_headless, re.I))
    print(f"visible-DOM 25k slice: static chars={len(slice_static)} sku_hits={len(hits_s)}/{len(static_skus or headless_skus)} loading={loading_s}")
    print(f"visible-DOM 25k slice: headless chars={len(slice_headless)} sku_hits={len(hits_h)}/{len(headless_skus or static_skus)} loading={loading_h}")
    print(f"static sku hits: {hits_s}")
    print(f"headless sku hits: {hits_h}")

    stop = False
    reasons = []
    if n_static != EXPECTED_N and n_headless != EXPECTED_N:
        stop = True
        reasons.append(f"oracle did not yield {EXPECTED_N} on either fetch (static={n_static}, headless={n_headless})")
    if len(hits_h) < EXPECTED_N:
        stop = True
        reasons.append(
            f"headless visible-DOM slice lacks full variant list "
            f"(sku hits {len(hits_h)}/{EXPECTED_N})"
        )

    llm_n = None
    report = None
    if len(hits_h) >= EXPECTED_N:
        print("=== PHASE 0: unified LLM extract on headless visible DOM ===")
        llm_rows = await extract_with_llm(headless.html or "", URL, ["sutures-surgical-products"])
        llm_n = len(llm_rows)
        llm_skus = [p.sku for p in llm_rows]
        print(f"LLM rows={llm_n} skus={llm_skus}")
        report = compare(llm_rows, oracle_headless or oracle_static)
        print(json.dumps(report, indent=2, default=str))
        diffs = []
        oracle_map = {normalize_sku(p.sku): p for p in (oracle_headless or oracle_static or []) if p.sku}
        for p in llm_rows:
            o = oracle_map.get(normalize_sku(p.sku))
            if not o:
                diffs.append({"sku": p.sku, "status": "llm-only"})
                continue
            field = report.get("field_match", {}).get(normalize_sku(p.sku), {})
            bad = [k for k, v in field.items() if not v]
            if bad:
                diffs.append({
                    "sku": p.sku,
                    "mismatched": bad,
                    "llm": {"name": p.name, "price": p.price, "currency": p.currency, "availability": p.availability, "desc?": bool(p.description), "images": len(p.image_urls)},
                    "oracle": {"name": o.name, "price": o.price, "currency": o.currency, "availability": o.availability, "desc?": bool(o.description), "images": len(o.image_urls)},
                })
        print("field diffs (mismatched only):")
        print(json.dumps(diffs, indent=2, default=str))
        if llm_n != EXPECTED_N:
            stop = True
            reasons.append(f"LLM enumerated {llm_n} rows, expected {EXPECTED_N}")
        (OUT / "surestitch_phase0_report.json").write_text(
            json.dumps(
                {
                    "url": URL,
                    "oracle_static": n_static,
                    "oracle_headless": n_headless,
                    "slice_sku_hits_static": len(hits_s),
                    "slice_sku_hits_headless": len(hits_h),
                    "llm_n": llm_n,
                    "compare": report,
                    "diffs": diffs,
                    "stop": stop,
                    "reasons": reasons,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    else:
        print("SKIP LLM extract: headless visible DOM does not carry all variant SKUs.")

    print("=== PHASE 0 VERDICT ===")
    print(f"STOP={stop}")
    for r in reasons:
        print(f"  - {r}")
    if not stop and llm_n == EXPECTED_N:
        print("GO: headless visible DOM + LLM enumerated 15==15. Safco extract REQUIRES headless render.")
    return 1 if stop else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
