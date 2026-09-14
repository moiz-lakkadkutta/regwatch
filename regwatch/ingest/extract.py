"""Tavily extract → Document rows. Lands Oct 18.

extract(urls≤20, extract_depth="advanced", format="markdown"); store markdown + sha256 content hash;
`diff.py` decides new / changed / unchanged by hash.
"""

from __future__ import annotations


def extract_documents(urls: list[str]) -> int:
    raise NotImplementedError("Oct 18 — see docs/regwatch-plan.md §4")
