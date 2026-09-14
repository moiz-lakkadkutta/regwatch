"""Tavily search over the fixed allow-list. Lands Oct 18 (docs/regwatch-plan.md).

search(query, search_depth="advanced", include_domains=[...], include_domains_mode="filter",
       time_range=..., max_results=10, include_raw_content=False)
Every response's `usage` is logged so the feed footer can show credits per crawl.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hit:
    url: str
    title: str
    published_date: str | None
    source_domain: str


def search_regulation(tag: str, query: str, *, time_range: str = "year") -> list[Hit]:
    raise NotImplementedError("Oct 18 — see docs/regwatch-plan.md §4")
