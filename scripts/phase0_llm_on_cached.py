"""Phase 0 step 4: LLM extract against cached headless HTML (no refetch)."""
from __future__ import annotations

import asyncio
import json
import os
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
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")

from app.tools.llm_extract import extract_with_llm
from app.tools.oracle import compare, normalize_sku
from app.tools.structured_data import structured_extract_product

URL = "https://www.safcodental.com/product/safco-surestitch-trade-sutures"
HTML = (ROOT / "data" / "phase0" / "surestitch_headless.html").read_text(encoding="utf-8")


async def main() -> None:
    oracle = structured_extract_product(HTML, URL)
    print("oracle n", len(oracle or []))
    rows = await extract_with_llm(HTML, URL, ["sutures-surgical-products"])
    print("llm n", len(rows))
    for p in rows:
        print(f"  sku={p.sku!r} name={p.name!r} price={p.price} currency={p.currency} avail={p.availability} imgs={len(p.image_urls)}")
    report = compare(rows, oracle)
    print(json.dumps(report, indent=2, default=str))
    (ROOT / "data" / "phase0" / "surestitch_llm_vs_oracle.json").write_text(
        json.dumps({"llm_n": len(rows), "oracle_n": len(oracle or []), "compare": report,
                    "llm": [{"sku": p.sku, "name": p.name, "price": p.price} for p in rows]},
                   indent=2, default=str),
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
