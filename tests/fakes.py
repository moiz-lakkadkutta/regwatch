"""Test doubles for the Tavily and LLM backend Protocols, plus scripted agent data."""

from __future__ import annotations

from typing import Any

from regwatch.ingest.tavily_backend import TavilyBackend, normalize_url


class RecordingBackend:
    """Wraps any TavilyBackend and records the kwargs of every call."""

    def __init__(self, inner: TavilyBackend) -> None:
        self.inner = inner
        self.search_calls: list[dict[str, Any]] = []
        self.extract_calls: list[dict[str, Any]] = []

    def search(self, **kw: Any) -> dict[str, Any]:
        self.search_calls.append(dict(kw))
        return self.inner.search(**kw)

    def extract(self, **kw: Any) -> dict[str, Any]:
        self.extract_calls.append(dict(kw))
        return self.inner.extract(**kw)


class ScriptedTavily:
    """Agent-side Tavily fake: answers any query. `results` maps an include_domains entry to its
    hits; `pages` maps a URL to its markdown. `search_credits` / `extract_credits` = None leaves
    `usage` out (credit fallback); `redirect` maps a requested URL to the URL the result reports."""

    def __init__(
        self,
        *,
        results: dict[str, list[dict[str, Any]]] | None = None,
        pages: dict[str, str] | None = None,
        search_credits: int | None = 1,
        extract_credits: int | None = 1,
        redirect: dict[str, str] | None = None,
    ) -> None:
        self.results = results or {}
        self.pages = {normalize_url(u): md for u, md in (pages or {}).items()}
        self.search_credits = search_credits
        self.extract_credits = extract_credits
        self.redirect = redirect or {}
        self.search_calls: list[dict[str, Any]] = []
        self.extract_calls: list[dict[str, Any]] = []

    def search(self, **kw: Any) -> dict[str, Any]:
        self.search_calls.append(dict(kw))
        resp: dict[str, Any] = {
            "query": kw["query"],
            "results": list(self.results.get(kw["include_domains"][0], [])),
            "response_time": 0.4,
        }
        if self.search_credits is not None:
            resp["usage"] = {"credits": self.search_credits}
        return resp

    def extract(self, **kw: Any) -> dict[str, Any]:
        self.extract_calls.append(dict(kw))
        results: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for url in kw["urls"]:
            md = self.pages.get(normalize_url(url))
            if md is None:
                failed.append({"url": url, "error": "not scripted"})
                continue
            results.append({"url": self.redirect.get(url, url), "raw_content": md, "images": []})
        resp: dict[str, Any] = {"results": results, "failed_results": failed, "response_time": 0.8}
        if self.extract_credits is not None:
            resp["usage"] = {"credits": self.extract_credits}
        return resp


IHK_URL = "https://www.ihk.de/eudr-kmu"
TRADER_QUOTE = (
    "Händler, die Holzprodukte nur von EU-Lieferanten kaufen, sind keine Marktteilnehmer im "
    "Sinne der Verordnung."
)
IHK_SENTENCE = "Kleinst- und Kleinunternehmen müssen die EUDR erst ab dem 30. Juni 2027 anwenden."
IHK_HIT: dict[str, Any] = {
    "title": "EUDR: Fristen für Kleinst- und Kleinunternehmen",
    "url": IHK_URL,
    "content": IHK_SENTENCE,
}
IHK_PAGE = f"# EUDR für KMU\n\n{IHK_SENTENCE}\n\n{TRADER_QUOTE}\n"


def ihk_tavily() -> ScriptedTavily:
    """One IHK hit on ihk.de and its page."""
    return ScriptedTavily(results={"ihk.de": [IHK_HIT]}, pages={IHK_URL: IHK_PAGE})
