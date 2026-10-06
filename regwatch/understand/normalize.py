"""Lightning: Document → RegChange with verbatim obligation quotes.

The model returns a `NormalizedPayload` (no `document_url`, string dates); we turn it into a
`RegChange` and run `RegChange.validate_quotes()` on the FULL markdown. Offending quotes trigger one
retry that lists them; whatever still fails afterwards is dropped (the whole document if nothing
grounded remains). Nothing is stored unless `validate_quotes()` is empty. Never relaxed in a prompt.

Long EUR-Lex acts (`is_long_act`) take `normalize_act`: one whole-act call for instrument,
jurisdiction and dates only, then one call per `split_act` section; each section with grounded
obligations becomes its own `RegChange` (`"<act> — <label>"`, same `document_url`).
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session

from regwatch.config import SECTION_SEP, Settings
from regwatch.db import documents_needing_normalize, store_regchange
from regwatch.schemas import Document, Obligation, RegChange
from regwatch.understand.llm_backend import (
    SOURCE_URL_PREFIX,
    LLMBackend,
    LLMUsage,
    ModelLedger,
    strict_schema,
)
from regwatch.understand.sections import Section, is_long_act, split_act

log = logging.getLogger(__name__)

SCHEMA_NAME = "regwatch_normalized_change"
MIN_QUOTE_WORDS = 6  # stricter than validate_quotes, never looser
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECTION_HINT = (
    "Section: {label} of {instrument}. Extract obligations from this section only; other "
    "sections of the act are processed separately. The amended act named in the section text "
    "decides who an obligation binds."
)
FALLBACK_JURISDICTION = "EU"
FALLBACK_INSTRUMENT_CHARS = 120


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


def build_messages(
    document: Document, *, max_chars: int, section: Section | None = None, instrument: str = ""
) -> tuple[list[dict[str, str]], bool]:
    """(messages, truncated). The user message starts and ends with `Source URL: <url>` (fixture
    key). With `section`, the document is the section's text, preceded by `SECTION_HINT`."""
    system, _ = load_prompt()
    markdown = section.text if section is not None else document.markdown
    hint = (
        SECTION_HINT.format(label=section.label, instrument=instrument) + "\n\n"
        if section is not None
        else ""
    )
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
        f"{hint}"
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
    section: str | None = None  # label when this is one section of a long act
    usage: LLMUsage = field(default_factory=LLMUsage)
    cost_usd: float = 0.0
    retried: bool = False

    @property
    def where(self) -> str:
        """Log suffix naming the section, empty for a whole document."""
        return f" [{self.section}]" if self.section else ""

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
            "model=%s prompt_tokens=%d completion_tokens=%d cost_usd=%.6f retried=%s url=%s%s",
            result.model,
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
            cost,
            self.retried,
            self.url,
            self.where,
        )
        return result.text

    def done(
        self, change: RegChange | None, *, error: str | None, dropped: list[str]
    ) -> NormalizeResult:
        if change is None:
            log.warning("not normalised (%s): %s%s", error, self.url, self.where)
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
    attempt = _Attempt(
        settings=settings, backend=backend, ledger=ledger, url=document.url, truncated=truncated
    )
    return _grounded(attempt, messages, document)


def _parsed(
    attempt: _Attempt, messages: list[dict[str, str]], url: str
) -> tuple[NormalizedPayload, str] | NormalizeResult:
    """First call plus at most one invalid-JSON retry: (payload, reply) or a failure."""
    _, _, json_addendum = _prompt_sections()
    try:
        reply = attempt.call(messages)
    except Exception as e:
        return attempt.fail(f"backend:{type(e).__name__}")
    try:
        return parse_payload(reply), reply
    except PayloadError as e:
        attempt.retried = True
        retry = _retry_messages(messages, reply, json_addendum.replace("{error}", e.detail), url)
    try:
        reply = attempt.call(retry)
    except Exception as exc:
        return attempt.fail(f"backend:{type(exc).__name__}")
    try:
        return parse_payload(reply), reply
    except PayloadError as e2:
        return attempt.fail(e2.kind)


def _grounded(
    attempt: _Attempt, messages: list[dict[str, str]], document: Document
) -> NormalizeResult:
    """Call, parse, validate quotes on the FULL `document.markdown`; one retry at most."""
    _, quotes_addendum, _ = _prompt_sections()
    parsed = _parsed(attempt, messages, document.url)
    if isinstance(parsed, NormalizeResult):
        return parsed
    payload, reply = parsed

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
            log.warning("dropped quote (%s%s): %.80s", document.url, attempt.where, q)
        kept = [o for o in change.obligations if o.quote not in dropped]
        if not kept:
            return attempt.fail("ungrounded_all_dropped", dropped)
        change = change.model_copy(update={"obligations": kept})

    # The grounding rule, enforced in code right before anything can be stored.
    if change.validate_quotes(document.markdown):
        return attempt.fail("ungrounded_all_dropped", dropped)
    return attempt.done(change, error=None, dropped=dropped)


@dataclass
class ActResult:
    """A long act: the whole-act call (metadata only) and one result per section, in order.

    `changes` are the stored-ready ones: each has ≥ 1 obligation grounded in the FULL markdown."""

    changes: list[RegChange]
    whole: NormalizeResult
    sections: list[tuple[Section, NormalizeResult]]

    @property
    def results(self) -> list[NormalizeResult]:
        return [self.whole, *(r for _, r in self.sections)]

    @property
    def retried(self) -> bool:
        """Any section needed its retry (the metadata-only whole-act call does not count)."""
        return any(r.retried for _, r in self.sections)

    @property
    def usage(self) -> LLMUsage:
        return LLMUsage(
            prompt_tokens=sum(r.usage.prompt_tokens for r in self.results),
            completion_tokens=sum(r.usage.completion_tokens for r in self.results),
        )

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)


_ACT_ID = re.compile(
    r"(?P<by>Commission )?(?P<kind>Delegated |Implementing )?"
    r"(?P<type>Directive|Regulation|Decision)"
    r" \((?P<body>EU|EC|EEC|Euratom)\) (?P<no>No )?(?P<num>\d{2,4}/\d+)",
    re.IGNORECASE,
)
_SHORT_CAP = 80


def short_act_name(instrument: str) -> str:
    """A short act identifier for section instruments, e.g. 'Directive (EU) 2023/2673'. The full
    official title steers the classifier ('… as regards financial services …'); the id does not.
    No recognisable id: the instrument cut before ' of the ', capped at 80 characters."""
    m = _ACT_ID.search(instrument)
    if m:
        body = m["body"].upper() if m["body"].lower() != "euratom" else "Euratom"
        parts = [
            (m["by"] or "").strip().capitalize(),
            (m["kind"] or "").strip().capitalize(),
            m["type"].capitalize(),
        ]
        head = " ".join(p for p in parts if p)
        no = "No " if m["no"] else ""
        return f"{head} ({body}) {no}{m['num']}"
    cut = re.split(r" of the ", instrument, maxsplit=1, flags=re.IGNORECASE)[0]
    return cut.strip()[:_SHORT_CAP]


def _fallback_instrument(markdown: str) -> str:
    """The act's first non-blank markdown line (heading marks stripped), capped."""
    for line in markdown.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:FALLBACK_INSTRUMENT_CHARS]
    return ""


def _act_metadata(
    document: Document, *, backend: LLMBackend, settings: Settings, ledger: ModelLedger
) -> NormalizeResult:
    """The whole-act call, metadata only: a change with instrument, jurisdiction and dates and NO
    obligations (the model's are discarded unchecked, so they cost no quote retry). Fails only on
    a backend error or a reply that is still unparseable after the JSON retry."""
    messages, truncated = build_messages(document, max_chars=settings.normalize_max_chars)
    attempt = _Attempt(
        settings=settings, backend=backend, ledger=ledger, url=document.url, truncated=truncated
    )
    parsed = _parsed(attempt, messages, document.url)
    if isinstance(parsed, NormalizeResult):
        return parsed
    change = to_regchange(parsed[0], document.url).model_copy(update={"obligations": []})
    return attempt.done(change, error=None, dropped=[])


def normalize_act(
    document: Document, *, backend: LLMBackend, settings: Settings, ledger: ModelLedger
) -> ActResult:
    """A long act → one grounded RegChange per section with obligations; never raises.

    The whole-act call (`_act_metadata`, truncated at `normalize_max_chars`) only supplies the
    instrument, jurisdiction and dates; its obligations are discarded without a quote check.
    Each `split_act` section is then normalised on its own text, sequentially, with quotes
    validated against the FULL markdown and the usual single retry. A section that fails on
    quotes/JSON/schema or yields nothing is skipped; a backend error in ANY section fails the
    whole act (no changes), so it is retried on the next crawl."""
    whole = _act_metadata(document, backend=backend, settings=settings, ledger=ledger)
    if whole.change is not None:
        instrument = whole.change.instrument
        jurisdiction = whole.change.jurisdiction
        dates = whole.change.effective_dates
    else:
        instrument = _fallback_instrument(document.markdown)
        jurisdiction, dates = FALLBACK_JURISDICTION, []
        log.warning("act metadata from fallback (%s): %s", instrument, document.url)

    short = short_act_name(instrument)
    changes: list[RegChange] = []
    sections: list[tuple[Section, NormalizeResult]] = []
    for section in split_act(document.markdown):
        messages, truncated = build_messages(
            document,
            max_chars=settings.normalize_max_chars,
            section=section,
            instrument=instrument,
        )
        attempt = _Attempt(
            settings=settings,
            backend=backend,
            ledger=ledger,
            url=document.url,
            truncated=truncated,
            section=section.label,
        )
        result = _grounded(attempt, messages, document)
        sections.append((section, result))
        if result.change is None:
            continue
        if not result.change.obligations:
            log.info("no obligations, not stored: %s [%s]", document.url, section.label)
            continue
        own = result.change
        changes.append(
            own.model_copy(
                update={
                    "instrument": f"{short}{SECTION_SEP}{section.label}",
                    "jurisdiction": (
                        jurisdiction
                        if own.jurisdiction.strip().lower() == "unclear"
                        else own.jurisdiction
                    ),
                    "effective_dates": own.effective_dates or dates,
                }
            )
        )
    n_failed = sum(r.change is None for _, r in sections)
    backend_errors = [
        f"{sec.label}: {r.error}"
        for sec, r in sections
        if r.error is not None and r.error.startswith("backend:")
    ]
    if backend_errors:
        # A transient backend error must not leave the act "done" with missing sections: store
        # nothing so the next crawl retries the whole act (dropped quotes/JSON stay per-section).
        log.warning(
            "act %s: backend error in %d of %d sections (%s); storing nothing, will retry",
            document.url,
            len(backend_errors),
            len(sections),
            "; ".join(backend_errors),
        )
        changes = []
    log.log(
        logging.WARNING if n_failed else logging.INFO,
        "act %s: %d of %d sections stored, %d failed",
        document.url,
        len(changes),
        len(sections),
        n_failed,
    )
    return ActResult(changes=changes, whole=whole, sections=sections)


@dataclass
class NormalizeSummary:
    """`n_retried` counts documents that were OK only after the retry; `n_failed` stored nothing.
    So n_docs == n_ok_first_try + n_retried + n_failed and n_ok == n_ok_first_try + n_retried.
    A long act is one document: ok if ≥ 1 section stored (retried if any section call retried);
    failed if no section stored or any section hit a backend error.
    `n_changes_new` counts stored changes, so one per stored section of a long act."""

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
    n_ok_first_try = n_retried = n_failed = n_changes_new = 0
    prompt_tokens = completion_tokens = 0
    cost = 0.0
    # Schemas are built here, on the calling thread; workers only see plain Documents. Each call
    # gets its own ledger, merged below in row order, so totals match a sequential run lock-free.
    jobs = [row.to_schema() for row in rows]
    workers = max(1, settings.normalize_concurrency)

    def run(doc: Document) -> tuple[NormalizeResult | ActResult | None, ModelLedger]:
        own = ModelLedger(ledger.price_in_per_mtok, ledger.price_out_per_mtok)
        try:
            if is_long_act(doc, settings):  # section calls run sequentially in this worker
                return normalize_act(doc, backend=backend, settings=settings, ledger=own), own
            return normalize(doc, backend=backend, settings=settings, ledger=own), own
        except Exception:
            log.exception("normalize crashed url=%s", doc.url)
            return None, own

    # No `with`: its exit waits for every queued call, so an error (or Ctrl-C) on this thread
    # would still pay for all remaining model calls. Cancel the queue instead.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for row, (result, own) in zip(rows, pool.map(run, jobs), strict=True):
            ledger.merge(own)
            if result is None:
                n_failed += 1
                continue
            prompt_tokens += result.usage.prompt_tokens
            completion_tokens += result.usage.completion_tokens
            cost += result.cost_usd
            if isinstance(result, ActResult):
                changes = result.changes
            else:
                changes = [result.change] if result.change is not None else []
            if not changes:
                n_failed += 1
                continue
            if result.retried:
                n_retried += 1
            else:
                n_ok_first_try += 1
            for change in changes:
                store_regchange(
                    s, change, content_hash=row.content_hash, model=settings.extractor_model
                )
            n_changes_new += len(changes)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    summary = NormalizeSummary(
        n_docs=len(rows),
        n_ok=n_ok_first_try + n_retried,
        n_ok_first_try=n_ok_first_try,
        n_retried=n_retried,
        n_failed=n_failed,
        n_changes_new=n_changes_new,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    log.info("%s", summary.line())
    return summary
