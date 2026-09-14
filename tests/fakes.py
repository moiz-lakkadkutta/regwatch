"""Test doubles for the Tavily backend Protocol."""

from __future__ import annotations

from typing import Any

from regwatch.ingest.tavily_backend import TavilyBackend


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
