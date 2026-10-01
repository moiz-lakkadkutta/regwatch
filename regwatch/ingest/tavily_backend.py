"""Tavily access behind a tiny Protocol so tests never import the SDK.

Two implementations: `LiveTavilyBackend` (the real SDK, imported lazily in `__init__`, refuses an
empty key because tavily-python 0.8.2 would silently go keyless and still hit the network) and
`FixtureTavilyBackend` (replays recorded-shape JSON under tests/fixtures/tavily).

Allow-list helpers live here too: `allowed_domain(url)` maps a URL host to its
`config.SOURCE_DOMAINS` key (exact host or a subdomain, plus the `SOURCE_URL_PREFIXES` path
rule for keys that have one), `normalize_url` is the dedupe key.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from regwatch.config import (
    SEED_REGULATIONS,
    SOURCE_DOMAINS,
    SOURCE_URL_PREFIXES,
    Settings,
    sources_for,
)

log = logging.getLogger(__name__)

SEARCH_CREDITS = 2  # advanced search
EXTRACT_CREDITS_PER_5 = 2  # advanced extract, per 5 URLs
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "tavily"


class TavilyBackend(Protocol):
    def search(self, **kw: Any) -> dict[str, Any]: ...

    def extract(self, **kw: Any) -> dict[str, Any]: ...


def _usage_credits(resp: dict[str, Any]) -> int | None:
    usage = resp.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("credits"), int | float):
        return int(usage["credits"])
    return None


@dataclass
class CreditLedger:
    """Credits spent in one crawl: the response's `usage` if present, else documented pricing."""

    credits: int = 0
    n_search_calls: int = 0
    n_extract_calls: int = 0

    def add_search(self, resp: dict[str, Any]) -> int:
        credits = _usage_credits(resp)
        if credits is None:
            credits = SEARCH_CREDITS
        self.credits += credits
        self.n_search_calls += 1
        return credits

    def add_extract(self, resp: dict[str, Any], n_urls: int) -> int:
        credits = _usage_credits(resp)
        if credits is None:
            credits = EXTRACT_CREDITS_PER_5 * math.ceil(n_urls / 5)
        self.credits += credits
        self.n_extract_calls += 1
        return credits


class LiveTavilyBackend:
    """The real SDK. `TavilyClient` is imported in `__init__`, not at module import."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError(
                "TAVILY_API_KEY is empty; tavily-python would go keyless and still call the network"
            )
        from tavily import TavilyClient

        self._client = TavilyClient(api_key=api_key)

    def search(self, **kw: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._client.search(**kw))

    def extract(self, **kw: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._client.extract(**kw))


class FixtureTavilyBackend:
    """Replays tests/fixtures/tavily: search/<tag>__<domain>.json, extract/<id>.md via manifest."""

    def __init__(self, root: Path) -> None:
        self.root = root
        manifest_path = root / "manifest.json"
        self.manifest: list[dict[str, Any]] = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
        )
        self._by_url: dict[str, dict[str, Any]] = {
            normalize_url(m["url"]): m for m in self.manifest
        }
        self._query_to_tag: dict[str, str] = {r["query"]: r["tag"] for r in SEED_REGULATIONS}

    def search(self, **kw: Any) -> dict[str, Any]:
        tag = self._query_to_tag[kw["query"]]
        domain = kw["include_domains"][0]
        path = self.root / "search" / f"{tag}__{domain}.json"
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def extract(self, **kw: Any) -> dict[str, Any]:
        urls: list[str] = list(kw["urls"])
        results: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for url in urls:
            entry = self._by_url.get(normalize_url(url))
            body = self.root / "extract" / f"{entry['id']}.md" if entry else None
            if body is None or not body.exists():
                failed.append({"url": url, "error": "not in fixtures"})
                continue
            results.append(
                {"url": url, "raw_content": body.read_text(encoding="utf-8"), "images": []}
            )
        return {
            "results": results,
            "failed_results": failed,
            "response_time": 1.2,
            "usage": {"credits": EXTRACT_CREDITS_PER_5 * math.ceil(len(urls) / 5)},
        }


def domains_for(tag: str) -> list[str]:
    return sources_for(tag)


def allowed_domain(url: str) -> str | None:
    """The `SOURCE_DOMAINS` key for this URL's host (exact or subdomain), else None.

    A key in `SOURCE_URL_PREFIXES` only matches when `host + path` starts with one of its prefixes.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    for d in SOURCE_DOMAINS:
        if host == d or host.endswith("." + d):
            prefixes = SOURCE_URL_PREFIXES.get(d)
            if prefixes is None or (host + parts.path).startswith(prefixes):
                return d
            return None
    return None


def is_allowed(url: str) -> bool:
    return allowed_domain(url) is not None


def normalize_url(url: str) -> str:
    """Dedupe key: lowercase scheme+host, no fragment, no utm_* params, no trailing slash."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = "&".join(p for p in parts.query.split("&") if p and not p.startswith("utm_"))
    return urlunsplit((scheme, netloc, path, query, ""))


def make_backend(
    settings: Settings,
    kind: Literal["live", "fixtures"],
    fixtures_dir: Path | None = None,
) -> TavilyBackend:
    if kind == "live":
        return LiveTavilyBackend(settings.tavily_api_key)
    return FixtureTavilyBackend(fixtures_dir or DEFAULT_FIXTURES_DIR)
