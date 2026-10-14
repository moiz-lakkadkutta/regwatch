"""The agent's Tavily tools and its merchant question. Code checks every argument the model
picks BEFORE any call. A domain must be a `config.SOURCE_DOMAINS` key. A URL must be one this run
was shown (a search hit or the change's own document) and pass `allowed_domain` and
`foreign_language` on both its raw and its normalised form. Results are checked again after the
call (search hits, extract redirects). Basic depth only. A refused call costs 0 credits; the caller
still counts it as a step.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from regwatch.config import SOURCE_DOMAINS
from regwatch.ingest.search import Hit, filter_hits
from regwatch.ingest.tavily_backend import (
    TavilyBackend,
    _usage_credits,
    allowed_domain,
    foreign_language,
    normalize_url,
)

log = logging.getLogger(__name__)

MAX_HITS = 5
SNIPPET_CHARS = 300
QUERY_CHARS = (3, 200)
QUESTION_CHARS = (10, 200)
OPTION_CHARS = (1, 80)
N_OPTIONS = (2, 4)
MAX_QUESTIONS = 2
HEAD_CHARS = 6_000
WINDOW_CHARS = 1_500
MAX_WINDOWS = 3
PAGE_VIEW_CHARS = 10_000
BASIC_CREDITS = 1  # basic search; basic extract of one URL; never settle below this
_WORD_RE = re.compile(r"[^\W\d_]{4,}")  # letters only, at least 4: query terms for passages


class Refused(Exception):  # noqa: N818 — a tool outcome shown to the model, not a crash
    """Refused in code before Tavily is called; `reason` is shown to the model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SearchResult:
    hits: list[Hit]
    credits: int


@dataclass(frozen=True)
class PageText:
    url: str
    source_domain: str
    markdown: str


@dataclass(frozen=True)
class ReadResult:
    page: PageText | None  # None: nothing usable came back (see `error`)
    credits: int
    error: str = ""


def _length_ok(value: str, bounds: tuple[int, int]) -> bool:
    return bounds[0] <= len(value) <= bounds[1]


def check_search(query: str | None, domain: str | None) -> tuple[str, str]:
    """(query, domain) or Refused; pure, runs before any Tavily call."""
    q = (query or "").strip()
    d = (domain or "").strip().lower()
    if d not in SOURCE_DOMAINS:
        raise Refused(f"domain {domain!r} is not on the allow-list")
    if not _length_ok(q, QUERY_CHARS):
        raise Refused(f"query must be {QUERY_CHARS[0]}–{QUERY_CHARS[1]} characters")
    return q, d


def check_read(url: str | None, *, allowed_urls: set[str]) -> tuple[str, str]:
    """(normalised url, source domain) or Refused. `allowed_urls` are normalised: this run's
    search hits plus the change's document_url. Language markers are checked on the raw URL too,
    because normalize_url drops prefLang/locale/lang."""
    raw = (url or "").strip()
    key = normalize_url(raw) if raw else ""
    if key not in allowed_urls:
        raise Refused("url was not in this run's search results or the change's source")
    domain = allowed_domain(key)
    if domain is None:
        raise Refused("url is not on the allow-list")
    for candidate in (raw, key):
        lang = foreign_language(candidate)
        if lang is not None:
            raise Refused(f"url is a {lang} language copy")
    return key, domain


def check_question(
    question: str | None, options: list[str] | None, *, n_asked: int
) -> tuple[str, list[str]]:
    """(question, options) or Refused: 10–200 chars, 2–4 distinct options of 1–80 chars,
    at most MAX_QUESTIONS per run."""
    if n_asked >= MAX_QUESTIONS:
        raise Refused(f"at most {MAX_QUESTIONS} questions per run")
    q = (question or "").strip()
    if not _length_ok(q, QUESTION_CHARS):
        raise Refused(f"question must be {QUESTION_CHARS[0]}–{QUESTION_CHARS[1]} characters")
    opts = [o.strip() for o in (options or [])]
    if not N_OPTIONS[0] <= len(opts) <= N_OPTIONS[1]:
        raise Refused(f"give {N_OPTIONS[0]}–{N_OPTIONS[1]} options")
    if not all(_length_ok(o, OPTION_CHARS) for o in opts):
        raise Refused(f"each option must be {OPTION_CHARS[0]}–{OPTION_CHARS[1]} characters")
    if len(set(opts)) != len(opts):
        raise Refused("options must differ")
    return q, opts


def search_sources(
    backend: TavilyBackend, *, query: str | None, domain: str | None, run_id: int
) -> SearchResult:
    """One basic search on one allow-listed domain; hits filtered by the shared `filter_hits`."""
    q, d = check_search(query, domain)
    resp = backend.search(
        query=q,
        search_depth="basic",
        include_domains=[d],
        include_domains_mode="filter",
        max_results=MAX_HITS,
        include_usage=True,
    )
    credits = _usage_credits(resp)
    if credits is None:
        credits = BASIC_CREDITS
    results = resp.get("results") or []
    log.info(
        "agent search run=%d domain=%s n_results=%d usage=%s credits=%d",
        run_id,
        d,
        len(results),
        resp.get("usage"),
        credits,
    )
    hits = [
        replace(h, title=h.title[:SNIPPET_CHARS])
        for h in filter_hits(
            results, tags=(), context=f"agent run={run_id} domain={d}", snippet_chars=SNIPPET_CHARS
        )
    ]
    return SearchResult(hits=hits[:MAX_HITS], credits=credits)


def read_source(
    backend: TavilyBackend, *, url: str | None, allowed_urls: set[str], run_id: int
) -> ReadResult:
    """One basic extract of one URL this run was shown; the result URL is re-checked (redirects)."""
    key, _ = check_read(url, allowed_urls=allowed_urls)
    resp = backend.extract(urls=[key], extract_depth="basic", format="markdown", include_usage=True)
    credits = max(BASIC_CREDITS, _usage_credits(resp) or 0)
    results = resp.get("results") or []
    log.info(
        "agent extract run=%d url=%s n_results=%d usage=%s credits=%d",
        run_id,
        key,
        len(results),
        resp.get("usage"),
        credits,
    )
    if not results:
        return ReadResult(page=None, credits=credits, error="no content came back for this url")
    got = str(results[0].get("url") or key)
    got_domain = allowed_domain(got)
    if got_domain is None or foreign_language(got) is not None:
        log.warning("agent: dropped extract off the allow-list run=%d url=%s", run_id, got)
        return ReadResult(
            page=None, credits=credits, error="the page redirected off the allow-list"
        )
    markdown = str(results[0].get("raw_content") or "")
    if not markdown.strip():
        return ReadResult(page=None, credits=credits, error="the page came back empty")
    return ReadResult(
        page=PageText(url=normalize_url(got), source_domain=got_domain, markdown=markdown),
        credits=credits,
    )


def search_view(hits: list[Hit], *, first_hit_id: int) -> list[dict[str, Any]]:
    """What the model sees of a search; hit ids are numbered across the run."""
    return [
        {"hit_id": first_hit_id + i, "title": h.title, "url": h.url, "snippet": h.snippet}
        for i, h in enumerate(hits)
    ]


def query_terms(queries: list[str]) -> list[str]:
    """Distinct words (letters only, at least 4) of the given queries, first seen first."""
    seen: dict[str, None] = {}
    for q in queries:
        for word in _WORD_RE.findall(q.lower()):
            seen.setdefault(word, None)
    return list(seen)


def page_view(page_id: int, page: PageText, terms: list[str]) -> dict[str, Any]:
    """What the model sees of a page: the first HEAD_CHARS characters, then up to MAX_WINDOWS
    non-overlapping windows of WINDOW_CHARS around the first later hit of each term, in term
    order; never more than PAGE_VIEW_CHARS in total. The full page stays in AgentPageRow."""
    text = page.markdown
    head = text[:HEAD_CHARS]
    lower = text.lower()
    budget = PAGE_VIEW_CHARS - len(head)
    spans: list[tuple[int, int]] = []
    passages: list[str] = []
    for term in terms:
        if len(passages) == MAX_WINDOWS or budget <= 0:
            break
        pos = lower.find(term, HEAD_CHARS)
        if pos == -1:
            continue
        start = max(HEAD_CHARS, pos - WINDOW_CHARS // 2)
        end = min(len(text), start + WINDOW_CHARS, start + budget)
        if any(start < e and s < end for s, e in spans):
            continue
        spans.append((start, end))
        passages.append(text[start:end])
        budget -= end - start
    return {
        "page_id": page_id,
        "url": page.url,
        "chars": len(text),
        "head": head,
        "passages": passages,
    }
