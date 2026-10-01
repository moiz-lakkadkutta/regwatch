"""Tavily extract → `Document`s, in batches of at most 20 URLs.

extract(urls≤20, extract_depth="advanced", format="markdown", include_usage=True); the markdown
is hashed with `diff.content_hash`. `failed_results` are logged and skipped. A result whose host is
not on the allow-list is dropped and logged; `source_domain` is always derived from the result URL
(never trusted from a caller).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from regwatch.ingest.diff import content_hash
from regwatch.ingest.search import Hit
from regwatch.ingest.tavily_backend import (
    CreditLedger,
    TavilyBackend,
    allowed_domain,
    foreign_language,
    normalize_url,
)
from regwatch.schemas import Document

log = logging.getLogger(__name__)

EXTRACT_BATCH = 20


def batched(urls: Sequence[str], n: int = EXTRACT_BATCH) -> list[list[str]]:
    if n < 1:
        raise ValueError("batch size must be >= 1")
    return [list(urls[i : i + n]) for i in range(0, len(urls), n)]


def _dedupe_by_content(docs: list[Document]) -> list[Document]:
    """Collapse identical `content_hash` into the shortest URL (tie: smallest), merging tags."""
    groups: dict[str, list[Document]] = {}
    for d in docs:
        groups.setdefault(d.content_hash, []).append(d)
    kept: dict[str, Document] = {}
    for h, group in groups.items():
        if len(group) == 1:
            if group[0].url in kept:
                log.warning("dedupe dropped same-url twin url=%s", group[0].url)
            kept[group[0].url] = group[0]
            continue
        best = min(group, key=lambda d: (len(d.url), d.url))
        tags = sorted({t for d in group for t in d.regulation_tags})
        published = next((d.published_date for d in group if d.published_date), None)
        winner = best.model_copy(update={"regulation_tags": tags, "published_date": published})
        log.info(
            "dedupe content_hash=%s kept=%s dropped=%s",
            h,
            best.url,
            [d.url for d in group if d is not best],
        )
        if best.url in kept:
            log.warning("dedupe dropped same-url twin url=%s", best.url)
        kept[best.url] = winner
    out = [kept.pop(d.url) for d in docs if d.url in kept]
    return out


def extract_documents(
    backend: TavilyBackend,
    hits: Sequence[Hit],
    *,
    ledger: CreditLedger,
    fetched_at: datetime | None = None,
) -> list[Document]:
    fetched_at = fetched_at or datetime.now(UTC)
    by_url: dict[str, Hit] = {}
    for h in hits:
        key = normalize_url(h.url)
        by_url.setdefault(key, h)
    docs: list[Document] = []
    for batch in batched(list(by_url)):
        try:
            resp = backend.extract(
                urls=batch, extract_depth="advanced", format="markdown", include_usage=True
            )
        except Exception:
            log.exception("extract failed n_urls=%d; skipping batch", len(batch))
            continue
        results = resp.get("results") or []
        failed = resp.get("failed_results") or []
        credits = ledger.add_extract(resp, len(batch))
        log.info(
            "extract n_urls=%d n_ok=%d n_failed=%d usage=%s credits=%d",
            len(batch),
            len(results),
            len(failed),
            resp.get("usage"),
            credits,
        )
        for f in failed:
            log.warning("extract failed url=%s error=%s", f.get("url"), f.get("error"))
        for r in results:
            raw_url = str(r.get("url", ""))
            source_domain = allowed_domain(raw_url)
            if source_domain is None:
                log.warning("dropped off-list extract result url=%s", raw_url)
                continue
            lang = foreign_language(raw_url)
            if lang is not None:
                log.warning("dropped foreign-language extract result lang=%s url=%s", lang, raw_url)
                continue
            url = normalize_url(raw_url)
            markdown = str(r.get("raw_content") or "")
            if not markdown.strip():
                log.warning("extract returned empty content url=%s", url)
                continue
            hit = by_url.get(url)
            docs.append(
                Document(
                    url=url,
                    content_hash=content_hash(markdown),
                    published_date=hit.published_date if hit else None,
                    fetched_at=fetched_at,
                    markdown=markdown,
                    source_domain=source_domain,
                    regulation_tags=sorted(hit.regulation_tags) if hit else [],
                )
            )
    return _dedupe_by_content(docs)
