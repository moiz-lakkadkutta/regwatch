"""Tavily search over the fixed allow-list, one call per (regulation tag, domain).

search(query, search_depth="advanced", include_domains=[domain], include_domains_mode="filter",
       time_range=..., max_results=10, include_raw_content=False, include_usage=True)
Every response's `usage` is logged and added to the `CreditLedger` so the feed footer can show
credits per crawl. Hits whose host is not on the allow-list are dropped and logged, never stored.
Non-en/de language copies are dropped the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace

from regwatch.config import SEED_REGULATIONS
from regwatch.ingest.tavily_backend import (
    CreditLedger,
    TavilyBackend,
    allowed_domain,
    domains_for,
    foreign_language,
    normalize_url,
)

log = logging.getLogger(__name__)

MAX_RESULTS = 10


@dataclass(frozen=True)
class Hit:
    url: str
    title: str
    published_date: str | None
    source_domain: str
    regulation_tags: tuple[str, ...]


def search_regulation(
    backend: TavilyBackend,
    tag: str,
    query: str,
    *,
    ledger: CreditLedger,
    time_range: str = "year",
) -> list[Hit]:
    """One search per allow-listed domain for `tag`. Failures are logged and skipped."""
    hits: list[Hit] = []
    for domain in domains_for(tag):
        try:
            resp = backend.search(
                query=query,
                search_depth="advanced",
                include_domains=[domain],
                include_domains_mode="filter",
                time_range=time_range,
                max_results=MAX_RESULTS,
                include_raw_content=False,
                include_usage=True,
            )
        except Exception:
            log.exception("search failed tag=%s domain=%s; skipping", tag, domain)
            continue
        results = resp.get("results") or []
        credits = ledger.add_search(resp)
        log.info(
            "search tag=%s domain=%s n_results=%d usage=%s credits=%d",
            tag,
            domain,
            len(results),
            resp.get("usage"),
            credits,
        )
        for r in results:
            url = str(r.get("url", ""))
            source_domain = allowed_domain(url)
            if source_domain is None:
                log.warning("dropped off-list hit tag=%s domain=%s url=%s", tag, domain, url)
                continue
            lang = foreign_language(url)
            if lang is not None:
                log.warning(
                    "dropped foreign-language hit tag=%s domain=%s lang=%s url=%s",
                    tag,
                    domain,
                    lang,
                    url,
                )
                continue
            hits.append(
                Hit(
                    url=normalize_url(url),
                    title=str(r.get("title") or ""),
                    published_date=r.get("published_date") or None,
                    source_domain=source_domain,
                    regulation_tags=(tag,),
                )
            )
    return hits


def dedupe_hits(hits: Iterable[Hit]) -> list[Hit]:
    """Collapse by `normalize_url`, merging regulation tags; sorted by url."""
    merged: dict[str, Hit] = {}
    for h in hits:
        key = normalize_url(h.url)
        prev = merged.get(key)
        if prev is None:
            merged[key] = replace(h, url=key)
            continue
        tags = tuple(sorted(set(prev.regulation_tags) | set(h.regulation_tags)))
        merged[key] = replace(
            prev,
            title=prev.title or h.title,
            published_date=prev.published_date or h.published_date,
            regulation_tags=tags,
        )
    return [merged[k] for k in sorted(merged)]


def search_all(
    backend: TavilyBackend, *, ledger: CreditLedger, time_range: str = "year"
) -> list[Hit]:
    """SEED_REGULATIONS × their allow-listed domains, deduped."""
    hits: list[Hit] = []
    for reg in SEED_REGULATIONS:
        hits.extend(
            search_regulation(
                backend, reg["tag"], reg["query"], ledger=ledger, time_range=time_range
            )
        )
    return dedupe_hits(hits)
