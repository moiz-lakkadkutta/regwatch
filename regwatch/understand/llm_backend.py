"""Token Factory (OpenAI-compatible chat completions) behind a Protocol; tests stay offline.

Two implementations: `LiveLLMBackend` (imports `openai` lazily in `__init__`, refuses an empty key)
and `FixtureLLMBackend` (replays recorded replies from `tests/fixtures/llm/<stage>/*.json`, keyed by
the `Source URL: <url>` line that ends the last user message). `ModelLedger` sums usage and cost
for one crawl; `strict_schema()` turns a pydantic model into a strict `response_format` schema.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from regwatch.config import Settings
from regwatch.ingest.tavily_backend import normalize_url

log = logging.getLogger(__name__)

DEFAULT_LLM_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "llm"
SOURCE_URL_PREFIX = "Source URL: "


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMResult(BaseModel):
    text: str
    usage: LLMUsage
    model: str


class LLMBackend(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None,
    ) -> LLMResult: ...


@dataclass
class ModelLedger:
    """Tokens and USD spent on one model in one crawl; crawl writes `cost_usd` to CrawlRunRow."""

    price_in_per_mtok: float
    price_out_per_mtok: float
    n_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, usage: LLMUsage) -> float:
        """Record one call; returns this call's cost in USD."""
        cost = (
            usage.prompt_tokens * self.price_in_per_mtok
            + usage.completion_tokens * self.price_out_per_mtok
        ) / 1_000_000
        self.n_calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cost_usd += cost
        return cost

    def merge(self, other: ModelLedger) -> None:
        """Fold another ledger's counters into this one (per-call ledgers from worker threads)."""
        self.n_calls += other.n_calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost_usd += other.cost_usd

    @classmethod
    def for_extractor(cls, settings: Settings) -> ModelLedger:
        return cls(
            price_in_per_mtok=settings.extractor_price_in_per_mtok,
            price_out_per_mtok=settings.extractor_price_out_per_mtok,
        )

    @classmethod
    def for_classifier(cls, settings: Settings) -> ModelLedger:
        return cls(
            price_in_per_mtok=settings.classifier_price_in_per_mtok,
            price_out_per_mtok=settings.classifier_price_out_per_mtok,
        )


class FixtureMissing(RuntimeError):  # noqa: N818 — name fixed by the plan
    """No (more) recorded replies for this Source URL."""


def source_url_of(messages: list[dict[str, str]]) -> str | None:
    """The `Source URL: <url>` line of the last user message (the fixture key)."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        for line in reversed(m.get("content", "").splitlines()):
            if line.startswith(SOURCE_URL_PREFIX):
                return line[len(SOURCE_URL_PREFIX) :].strip()
        return None
    return None


class FixtureLLMBackend:
    """Replays `<root>/*.json`; each file has "url" and "responses" [{text, usage}, ...].

    Keyed by `normalize_url(url)`, like the Tavily fixture backend, so a document stored under its
    canonical URL still finds the reply recorded for the manifest spelling.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[dict[str, str]]] = []
        self._responses: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(root.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = normalize_url(payload["url"])
            self._responses.setdefault(key, []).extend(payload["responses"])

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None,
    ) -> LLMResult:
        self.calls.append([dict(m) for m in messages])
        url = source_url_of(messages)
        if url is None:
            raise FixtureMissing("last user message has no 'Source URL: <url>' line")
        queue = self._responses.get(normalize_url(url))
        if not queue:
            raise FixtureMissing(f"no recorded reply left for {url} under {self.root}")
        reply = queue.pop(0)
        return LLMResult(
            text=reply["text"],
            usage=LLMUsage.model_validate(reply.get("usage") or {}),
            model=reply.get("model", model),
        )


class LiveLLMBackend:
    """Token Factory via the OpenAI SDK; the SDK is imported here, not at module import."""

    def __init__(self, settings: Settings) -> None:
        if not settings.nebius_api_key:
            raise ValueError("NEBIUS_API_KEY is empty; refusing to create a live LLM backend")
        from openai import OpenAI

        self._client: Any = OpenAI(
            base_url=settings.tokenfactory_base_url, api_key=settings.nebius_api_key
        )

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None,
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 4096,
        }
        try:
            if response_format is not None:
                resp = self._client.chat.completions.create(
                    **kwargs, response_format=response_format
                )
            else:
                resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            if (
                response_format is not None
                and getattr(e, "status_code", None) == 400
                and "response_format" in str(e)
            ):
                log.warning(
                    "model %s rejected response_format (%s); retrying once without it", model, e
                )
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise
        text = resp.choices[0].message.content or ""
        if resp.usage is None:
            log.warning("model %s returned no usage; counting zero tokens", model)
            usage = LLMUsage()
        else:
            usage = LLMUsage(
                prompt_tokens=int(resp.usage.prompt_tokens or 0),
                completion_tokens=int(resp.usage.completion_tokens or 0),
            )
        return LLMResult(text=text, usage=usage, model=str(resp.model or model))


def _strip_schema(node: Any) -> None:
    """In place: drop title/default everywhere, require every property (strict-mode rules)."""
    if isinstance(node, dict):
        node.pop("title", None)
        node.pop("default", None)
        props = node.get("properties")
        if isinstance(props, dict):
            node["required"] = list(props)
            for sub in props.values():
                _strip_schema(sub)
        for key, value in node.items():
            if key == "properties":
                continue
            _strip_schema(value)
    elif isinstance(node, list):
        for item in node:
            _strip_schema(item)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model.model_json_schema()` made strict: no title/default/format, all properties required."""
    schema = model.model_json_schema()
    _strip_schema(schema)
    dumped = json.dumps(schema)
    if '"format"' in dumped:
        raise ValueError(f"{model.__name__}: strict schemas must not carry 'format' keys")
    return schema


LLMStage = Literal["normalize", "classify"]


def make_llm_backend(
    kind: Literal["live", "fixtures"],
    settings: Settings,
    fixtures_dir: Path | None = None,
    *,
    stage: LLMStage = "normalize",
) -> LLMBackend:
    """`fixtures_dir` is the parent of the per-stage folders (tests/fixtures/llm/<stage>).

    One live backend serves every stage; a fixture backend replays one stage's folder.
    """
    if kind == "live":
        return LiveLLMBackend(settings)
    return FixtureLLMBackend((fixtures_dir or DEFAULT_LLM_FIXTURES_DIR) / stage)
