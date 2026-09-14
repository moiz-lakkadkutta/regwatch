"""Lightning: Document → RegChange with verbatim obligation quotes.

The model returns a `NormalizedPayload` (no `document_url`, string dates); we turn it into a
`RegChange` and run `RegChange.validate_quotes()` on the FULL markdown. Offending quotes trigger one
retry that lists them; whatever still fails afterwards is dropped (the whole document if nothing
grounded remains). Nothing is stored unless `validate_quotes()` is empty. Never relaxed in a prompt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session

from regwatch.config import Settings
from regwatch.db import documents_needing_normalize, store_regchange
from regwatch.schemas import Document, Obligation, RegChange
from regwatch.understand.llm_backend import (
    SOURCE_URL_PREFIX,
    LLMBackend,
    LLMUsage,
    ModelLedger,
    strict_schema,
)

log = logging.getLogger(__name__)

SCHEMA_NAME = "regwatch_normalized_change"
MIN_QUOTE_WORDS = 6  # stricter than validate_quotes, never looser
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ObligationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    quote: str


class NormalizedPayload(BaseModel):
    """What the MODEL returns; `document_url` is ours, `effective_dates` are strings."""

    model_config = ConfigDict(extra="forbid")

    instrument: str
    jurisdiction: str
    summary: str
    effective_dates: list[str] = Field(default_factory=list)
    obligations: list[ObligationOut]


class PayloadError(ValueError):
    """The reply was not a `NormalizedPayload`; `kind` is 'invalid_json' or 'schema'."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def response_json_schema() -> dict[str, Any]:
    return strict_schema(NormalizedPayload)


def response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": response_json_schema(), "strict": True},
    }


@cache
def _prompt_sections() -> tuple[str, str, str]:
    raw = resources.files("regwatch.prompts").joinpath("normalize.md").read_text(encoding="utf-8")
    system, _, rest = raw.partition("\n## Retry addendum\n")
    quotes_addendum, _, json_addendum = rest.partition("\n## Retry addendum (invalid JSON)\n")
    schema_text = json.dumps(response_json_schema(), indent=2, ensure_ascii=False)
    return (
        system.replace("{schema}", schema_text).strip(),
        quotes_addendum.strip(),
        json_addendum.strip(),
    )


def load_prompt() -> tuple[str, str]:
    """(system prompt with the JSON schema pasted in, retry addendum template for bad quotes)."""
    system, quotes_addendum, _ = _prompt_sections()
    return system, quotes_addendum


def build_messages(document: Document, *, max_chars: int) -> tuple[list[dict[str, str]], bool]:
    """(messages, truncated). The user message ends with `Source URL: <url>` (fixture key)."""
    system, _ = load_prompt()
    markdown = document.markdown
    truncated = len(markdown) > max_chars
    if truncated:
        log.warning(
            "truncating %s from %d to %d chars for the model (quotes validated on full text)",
            document.url,
            len(markdown),
            max_chars,
        )
        markdown = markdown[:max_chars]
    user = (
        f"{SOURCE_URL_PREFIX}{document.url}\n"
        f"Source domain: {document.source_domain}\n"
        f"Published: {document.published_date or 'unknown'}\n\n"
        f"<document>\n{markdown}\n</document>\n\n"
        f"{SOURCE_URL_PREFIX}{document.url}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], truncated


def _retry_messages(
    messages: list[dict[str, str]], previous_reply: str, addendum: str, url: str
) -> list[dict[str, str]]:
    return [
        *messages,
        {"role": "assistant", "content": previous_reply},
        {"role": "user", "content": f"{addendum}\n\n{SOURCE_URL_PREFIX}{url}"},
    ]


def parse_payload(text: str) -> NormalizedPayload:
    """Tolerates <think> blocks, ``` fences and prose around the JSON; raises PayloadError."""
    body = _THINK_RE.sub("", text)
    fenced = _FENCE_RE.search(body)
    if fenced:
        body = fenced.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise PayloadError("invalid_json", "no JSON object in reply")
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError as e:
        raise PayloadError("invalid_json", str(e)) from e
    try:
        return NormalizedPayload.model_validate(data)
    except ValidationError as e:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise PayloadError("schema", errors) from e


def to_regchange(payload: NormalizedPayload, document_url: str) -> RegChange:
    """Keeps only YYYY-MM-DD dates (WARNING per drop); `document_url` is ours, never the model's."""
    dates: list[date] = []
    for raw in payload.effective_dates:
        candidate = raw.strip()
        try:
            if not _ISO_DATE_RE.match(candidate):
                raise ValueError("not YYYY-MM-DD")
            dates.append(date.fromisoformat(candidate))
        except ValueError:
            log.warning("dropped unparsable effective date %r for %s", raw, document_url)
    return RegChange(
        document_url=document_url,
        instrument=payload.instrument,
        jurisdiction=payload.jurisdiction,
        summary=payload.summary,
        effective_dates=dates,
        obligations=[Obligation(text=o.text, quote=o.quote) for o in payload.obligations],
    )


def offending_quotes(change: RegChange, markdown: str) -> list[str]:
    """Quotes that `validate_quotes` rejects on the FULL markdown, plus any shorter than 6 words."""
    failed = change.validate_quotes(markdown)
    short = [o.quote for o in change.obligations if len(o.quote.split()) < MIN_QUOTE_WORDS]
    return list(dict.fromkeys([*failed, *short]))


@dataclass
class NormalizeResult:
    change: RegChange | None
    retried: bool
    dropped_quotes: list[str]
    usage: LLMUsage
    cost_usd: float
    truncated: bool
    error: str | None  # invalid_json | schema | ungrounded_all_dropped | backend:<ExcName>


@dataclass
class _Attempt:
    """Bookkeeping across the (at most) two model calls of one document."""

    settings: Settings
    backend: LLMBackend
    ledger: ModelLedger
    url: str
    truncated: bool
    usage: LLMUsage = field(default_factory=LLMUsage)
    cost_usd: float = 0.0
    retried: bool = False

    def call(self, messages: list[dict[str, str]]) -> str:
        result = self.backend.complete(
            model=self.settings.extractor_model,
            messages=messages,
            response_format=response_format(),
        )
        cost = self.ledger.add(result.usage)
        self.usage = LLMUsage(
            prompt_tokens=self.usage.prompt_tokens + result.usage.prompt_tokens,
            completion_tokens=self.usage.completion_tokens + result.usage.completion_tokens,
        )
        self.cost_usd += cost
        log.info(
            "model=%s prompt_tokens=%d completion_tokens=%d cost_usd=%.6f retried=%s url=%s",
            result.model,
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
            cost,
            self.retried,
            self.url,
        )
        return result.text

    def done(
        self, change: RegChange | None, *, error: str | None, dropped: list[str]
    ) -> NormalizeResult:
        if change is None:
            log.warning("not normalised (%s): %s", error, self.url)
        return NormalizeResult(
            change=change,
            retried=self.retried,
            dropped_quotes=dropped,
            usage=self.usage,
            cost_usd=self.cost_usd,
            truncated=self.truncated,
            error=error,
        )

    def fail(self, error: str, dropped: list[str] | None = None) -> NormalizeResult:
        return self.done(None, error=error, dropped=dropped or [])


def normalize(
    document: Document, *, backend: LLMBackend, settings: Settings, ledger: ModelLedger
) -> NormalizeResult:
    """One document → grounded RegChange or a failure; one retry at most; never raises."""
    messages, truncated = build_messages(document, max_chars=settings.normalize_max_chars)
    _, quotes_addendum, json_addendum = _prompt_sections()
    attempt = _Attempt(
        settings=settings, backend=backend, ledger=ledger, url=document.url, truncated=truncated
    )

    try:
        reply = attempt.call(messages)
    except Exception as e:
        return attempt.fail(f"backend:{type(e).__name__}")

    try:
        payload = parse_payload(reply)
    except PayloadError as e:
        attempt.retried = True
        retry = _retry_messages(
            messages, reply, json_addendum.replace("{error}", e.detail), document.url
        )
        try:
            reply = attempt.call(retry)
        except Exception as exc:
            return attempt.fail(f"backend:{type(exc).__name__}")
        try:
            payload = parse_payload(reply)
        except PayloadError as e2:
            return attempt.fail(e2.kind)

    change = to_regchange(payload, document.url)
    failed = offending_quotes(change, document.markdown)
    if failed and not attempt.retried:
        attempt.retried = True
        listed = "\n".join(f'- "{q}"' for q in failed)
        retry = _retry_messages(
            messages, reply, quotes_addendum.replace("{failed_quotes}", listed), document.url
        )
        try:
            reply = attempt.call(retry)
        except Exception as exc:
            return attempt.fail(f"backend:{type(exc).__name__}")
        try:
            payload = parse_payload(reply)
        except PayloadError as e:
            return attempt.fail(e.kind)
        change = to_regchange(payload, document.url)
        failed = offending_quotes(change, document.markdown)

    dropped = failed
    if dropped:
        for q in dropped:
            log.warning("dropped quote (%s): %.80s", document.url, q)
        kept = [o for o in change.obligations if o.quote not in dropped]
        if not kept:
            return attempt.fail("ungrounded_all_dropped", dropped)
        change = change.model_copy(update={"obligations": kept})

    # The grounding rule, enforced in code right before anything can be stored.
    if change.validate_quotes(document.markdown):
        return attempt.fail("ungrounded_all_dropped", dropped)
    return attempt.done(change, error=None, dropped=dropped)


@dataclass
class NormalizeSummary:
    """`n_retried` counts documents that were OK only after the retry; `n_failed` stored nothing.
    So n_docs == n_ok_first_try + n_retried + n_failed and n_ok == n_ok_first_try + n_retried."""

    n_docs: int
    n_ok: int
    n_ok_first_try: int
    n_retried: int
    n_failed: int
    n_changes_new: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int

    @property
    def success_rate_no_retry(self) -> float:
        return self.n_ok_first_try / self.n_docs if self.n_docs else 0.0

    def line(self) -> str:
        pct = round(self.success_rate_no_retry * 100)
        return (
            f"normalize: {self.n_docs} documents · {self.n_ok_first_try} ok first try ({pct}%) · "
            f"{self.n_retried} retried · {self.n_failed} failed · ${self.cost_usd:.4f} model"
        )


def normalize_all(
    s: Session,
    *,
    backend: LLMBackend,
    settings: Settings,
    ledger: ModelLedger | None = None,
) -> NormalizeSummary:
    """Normalise every DocumentRow without a RegChangeRow for its (url, content_hash) and store the
    grounded ones. Failures store nothing and are retried on the next crawl."""
    ledger = ledger or ModelLedger.for_extractor(settings)
    rows = documents_needing_normalize(s)
    n_ok_first_try = n_retried = n_failed = 0
    prompt_tokens = completion_tokens = 0
    cost = 0.0
    for row in rows:
        result = normalize(row.to_schema(), backend=backend, settings=settings, ledger=ledger)
        prompt_tokens += result.usage.prompt_tokens
        completion_tokens += result.usage.completion_tokens
        cost += result.cost_usd
        if result.change is None:
            n_failed += 1
            continue
        if result.retried:
            n_retried += 1
        else:
            n_ok_first_try += 1
        store_regchange(
            s, result.change, content_hash=row.content_hash, model=settings.extractor_model
        )
    summary = NormalizeSummary(
        n_docs=len(rows),
        n_ok=n_ok_first_try + n_retried,
        n_ok_first_try=n_ok_first_try,
        n_retried=n_retried,
        n_failed=n_failed,
        n_changes_new=n_ok_first_try + n_retried,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    log.info("%s", summary.line())
    return summary
