"""Host -> SiteAdapter registry. NET32_GENERALIZATION_KICKOFF.md §4.1:
"resolve the adapter by URL host... the existing UI category-URL input
already accepts arbitrary URLs — just route to the right adapter by host."

The only place a host string is allowed to appear as node-level logic: here,
and only here. app/nodes/* never branch on hostname.
"""
from __future__ import annotations

from urllib.parse import urlparse

from app.schemas import SiteAdapter
from config.net32 import NET32_ADAPTER
from config.safco import SAFCO_ADAPTER

_ADAPTERS: dict[str, SiteAdapter] = {
    urlparse(a.base_url).netloc.removeprefix("www."): a
    for a in (SAFCO_ADAPTER, NET32_ADAPTER)
}


def resolve_adapter(url: str) -> SiteAdapter | None:
    host = urlparse(url).netloc.removeprefix("www.")
    return _ADAPTERS.get(host)


def resolve_adapter_for_urls(urls: list[str]) -> SiteAdapter:
    """Resolve one adapter for a batch of category URLs. Raises ValueError if
    the batch is empty, spans hosts we don't have an adapter for, or mixes
    hosts — a single run is bound to one SiteAdapter (one render mode, one
    pagination scheme, one rate limit)."""
    if not urls:
        raise ValueError("no category URLs given")
    hosts = {urlparse(u).netloc.removeprefix("www.") for u in urls}
    if len(hosts) > 1:
        raise ValueError(f"category URLs span multiple hosts in one run: {sorted(hosts)}")
    adapter = resolve_adapter(urls[0])
    if adapter is None:
        raise ValueError(f"no SiteAdapter registered for host {next(iter(hosts))!r}")
    return adapter
