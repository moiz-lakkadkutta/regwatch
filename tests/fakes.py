"""Test doubles for the Tavily and LLM backend Protocols, plus scripted agent data."""

from __future__ import annotations

import json
from typing import Any

from regwatch.ingest.tavily_backend import TavilyBackend, normalize_url
from regwatch.understand.llm_backend import FixtureMissing, LLMResult, LLMUsage


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


class ScriptedLLM:
    """LLMBackend replaying scripted replies in order, whatever the Source URL. It records every
    call's messages and response_format; once the replies run out it raises FixtureMissing, so the
    run fails visibly."""

    def __init__(
        self, replies: list[str], *, prompt_tokens: int = 2_000, completion_tokens: int = 150
    ) -> None:
        self.replies = list(replies)
        self.usage = LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        self.calls: list[list[dict[str, str]]] = []
        self.formats: list[dict[str, Any] | None] = []

    def complete(
        self, *, model: str, messages: list[dict[str, str]], response_format: dict[str, Any] | None
    ) -> LLMResult:
        self.calls.append([dict(m) for m in messages])
        self.formats.append(response_format)
        if not self.replies:
            raise FixtureMissing("no scripted agent reply left")
        return LLMResult(text=self.replies.pop(0), usage=self.usage, model=model)


def agent_action(tool: str, thought: str = "next step", **fields: Any) -> str:
    """One AgentAction reply as JSON: every field present, the unused ones null."""
    body: dict[str, Any] = dict.fromkeys(
        ("query", "domain", "url", "question", "options")
        + ("decision", "reasons", "todos", "evidence")
    )
    body.update(fields)
    return json.dumps({"thought": thought, "tool": tool, **body}, ensure_ascii=False)


QUESTION = "Do you import wood or leather goods from outside the EU yourself?"
OPTIONS = ["Yes, I import some myself", "No, I buy only from EU suppliers"]


def until_question() -> list[str]:
    """search ihk.de → read the IHK page → ask the merchant (the run then waits)."""
    return [
        agent_action(
            "search_sources",
            "Check whether a shop buying in the EU counts as an operator.",
            query="EUDR Händler EU-Lieferanten Marktteilnehmer",
            domain="ihk.de",
        ),
        agent_action("read_source", "The IHK page covers traders.", url=IHK_URL),
        agent_action(
            "ask_merchant", "Whether you import decides it.", question=QUESTION, options=OPTIONS
        ),
    ]


def decide_does_not(*, fact_id: int = 1, page_id: int = 1) -> str:
    """does_not, citing the merchant's answer, the IHK quote and obligation 4 (due diligence)."""
    return agent_action(
        "decide",
        "A shop buying only in the EU is a trader, not an operator.",
        decision="does_not",
        reasons=[
            {
                "text": "You buy only from EU suppliers, so these operator duties don't bind you.",
                "fact_ids": [fact_id],
                "evidence_ids": [1],
                "obligation_numbers": [4],
            }
        ],
        todos=[],
        evidence=[{"evidence_id": 1, "page_id": page_id, "quote": TRADER_QUOTE}],
    )


def decide_unclear() -> str:
    """Keeps unclear, backed by obligation 1 of the EUDR change."""
    return agent_action(
        "decide",
        "The sources do not settle who places the goods on the market.",
        decision="unclear",
        reasons=[
            {
                "text": "It depends on whether you place the goods on the EU market yourself.",
                "fact_ids": [],
                "evidence_ids": [],
                "obligation_numbers": [1],
            }
        ],
        todos=[],
        evidence=[],
    )
