"""Five Pydantic v2 schemas that are the data contract for the whole system.

Frozen per BUILD_SPEC.md §7. Missing fields become explicit nulls — never fabricated.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class PageType(str, Enum):
    CATEGORY = "category"
    LISTING = "listing"
    PRODUCT = "product"
    UNKNOWN = "unknown"


class RenderMode(str, Enum):
    STATIC = "static"
    HEADLESS = "headless"


class ExtractionMethod(str, Enum):
    STRUCTURED = "structured"
    SELECTOR = "selector"
    LLM = "llm"
    NONE = "none"


class PriceStatus(str, Enum):
    VISIBLE = "visible"
    GATED = "gated"
    NOT_FOUND = "not_found"


class Availability(str, Enum):
    IN_STOCK = "In stock"
    PARTIALLY_IN_STOCK = "Partially in stock"
    BACKORDERED = "Backordered"
    SPECIAL_ORDER = "Special order"
    OUT_OF_STOCK = "Out of stock"
    UNKNOWN = "Unknown"


class JobType(str, Enum):
    CATEGORY = "category"
    LISTING = "listing"
    PRODUCT = "product"


class RecoveryStage(str, Enum):
    FETCH = "fetch"
    CLASSIFY = "classify"
    EXTRACT = "extract"
    VALIDATE = "validate"
    STORE = "store"


# ---------------------------------------------------------------------------
# A. LoopTrace — one per iteration, observability
# ---------------------------------------------------------------------------

class LoopTrace(BaseModel):
    trace_id: str = Field(default_factory=new_id)
    job_id: str
    url: str
    page_type: Optional[PageType] = None
    reasoning: Optional[str] = None
    decision: Optional[str] = None
    confidence: Optional[float] = None
    render_mode: Optional[RenderMode] = None
    extraction_method: Optional[ExtractionMethod] = None
    timestamp: str = Field(default_factory=now_iso)
    # Path to the raw/cleaned HTML blob on disk, not inlined here.
    html_blob_path: Optional[str] = None


# ---------------------------------------------------------------------------
# B. Product — normalized output, one row per variant
# ---------------------------------------------------------------------------

class Product(BaseModel):
    product_id: str = Field(default_factory=new_id)
    variant_id: Optional[str] = None
    name: Optional[str] = None
    brand: Optional[str] = None
    sku: Optional[str] = None
    category_path: list[str] = Field(default_factory=list)
    url: Optional[str] = None          # canonical URL
    crawl_url: Optional[str] = None    # URL actually fetched
    price: Optional[float] = None
    currency: Optional[str] = None
    price_status: PriceStatus = PriceStatus.NOT_FOUND
    pack_size: Optional[str] = None
    availability: Availability = Availability.UNKNOWN
    description: Optional[str] = None
    specifications: dict[str, str] = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    extraction_method: ExtractionMethod = ExtractionMethod.NONE
    scraped_at: str = Field(default_factory=now_iso)
    source_hash: Optional[str] = None

    def compute_source_hash(self) -> str:
        basis = "|".join([
            self.name or "", self.sku or "", str(self.price), self.availability.value,
            self.description or "",
        ])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# C. Job — the queue unit
# ---------------------------------------------------------------------------

class Job(BaseModel):
    job_id: str = Field(default_factory=new_id)
    url: str
    type: JobType
    category_path: list[str] = Field(default_factory=list)
    parent_url: Optional[str] = None
    depth: int = 0
    attempts: int = 0
    render_mode: RenderMode = RenderMode.STATIC


# ---------------------------------------------------------------------------
# D. FailureRecord — dead-letter record, absolute fallback
# ---------------------------------------------------------------------------

class FailureRecord(BaseModel):
    job_id: str
    url: str
    stage: RecoveryStage
    error: str
    attempts: int
    last_render_mode: Optional[RenderMode] = None
    timestamp: str = Field(default_factory=now_iso)


# ---------------------------------------------------------------------------
# E. SiteAdapter — per-site config as data, not code
# ---------------------------------------------------------------------------

class PaginationConfig(BaseModel):
    type: str = "query_param"
    param: str = "p"


class SiteAdapter(BaseModel):
    base_url: str
    allowed_paths: list[str] = Field(default_factory=list)
    rate_limit: float = 2.0  # seconds between requests
    start_render_mode: RenderMode = RenderMode.STATIC
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    selectors: dict[str, str] = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=list)
    max_products_per_category: int = 25
    max_pages: int = 200
