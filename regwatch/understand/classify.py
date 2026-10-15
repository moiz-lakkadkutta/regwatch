"""Ultra: (RegChange, Profile) → Applicability with grounded to-dos.

The model returns a `ClassifiedPayload` (decision, reasons, to-dos with string dates and no
`citation_url`). `enforce()` turns it into an `Applicability` in code: the citation is ours, an
invented `due` becomes None, `does_not` carries no to-dos, `unclear` at most one, and every to-do
whose quote is not one of the change's obligation quotes is rejected. One retry lists the rejected
to-dos; whatever is still rejected afterwards is dropped. `Applicability.ungrounded_todos()` must be
empty before anything is stored. Never relaxed in a prompt. Nothing here is regulation-specific.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlmodel import Session

from regwatch.config import CATEGORIES, EU_COUNTRIES, PLATFORMS, Settings, country_name
from regwatch.db import ProfileRow, regchanges_needing_classify, store_applicability
from regwatch.moves import RuleMove, instrument_moves
from regwatch.schemas import Applicability, Decision, Profile, RegChange, Todo
from regwatch.understand.llm_backend import (
    SOURCE_URL_PREFIX,
    LLMBackend,
    LLMUsage,
    ModelLedger,
    strict_schema,
)
from regwatch.understand.normalize import _FENCE_RE, _THINK_RE, PayloadError

log = logging.getLogger(__name__)

SCHEMA_NAME = "classified_payload"
MAX_TODOS = 6  # obligations turned into to-dos (one per product group when dates differ)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CATEGORY_LABELS = dict(CATEGORIES)
_PLATFORM_LABELS = dict(PLATFORMS)
# Stated as the facts the law turns on; the form words them for the owner (config).
_SIZE_FACTS = {
    "small": "micro or small enterprise (fewer than 50 staff, turnover up to €10 million)",
    "larger": "medium-sized or large enterprise",
}
_SUPPLY_FACTS = {
    "eu_suppliers": (
        "bought from suppliers established in the EU (the shop does not import or make them)"
    ),
    "imports_or_makes": "imported by the shop from outside the EU, or made by the shop itself",
}


class TodoDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    due: str | None = None
    quote: str


class ClassifiedPayload(BaseModel):
    """What the MODEL returns; `citation_url` is deliberately absent (ours), `due` is a string."""

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    reasons: list[str]
    todos: list[TodoDraft]


def response_json_schema() -> dict[str, Any]:
    return strict_schema(ClassifiedPayload)


def response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": response_json_schema(), "strict": True},
    }


@cache
def load_prompt() -> str:
    """System prompt from prompts/classify.md with the strict JSON schema appended."""
    raw = resources.files("regwatch.prompts").joinpath("classify.md").read_text(encoding="utf-8")
    schema_text = json.dumps(response_json_schema(), indent=2, ensure_ascii=False)
    return f"{raw.strip()}\n\n## JSON schema\n{schema_text}"


def render_profile(profile: Profile) -> str:
    if set(profile.ship_to) >= set(EU_COUNTRIES):
        ships_to = f"all {len(EU_COUNTRIES)} EU countries"
    else:
        ships_to = ", ".join(f"{country_name(c)} ({c})" for c in profile.ship_to) or "none given"
    sells = "; ".join(f"{_CATEGORY_LABELS.get(c, c)} ({c})" for c in profile.categories)
    return (
        "## Store profile\n"
        f"- Established in: {country_name(profile.establishment_country)} "
        f"({profile.establishment_country})\n"
        f"- Ships to: {ships_to}\n"
        f"- Sells: {sells or 'none given'}\n"
        f"- Platform: {_PLATFORM_LABELS.get(profile.platform, profile.platform)}\n"
        f"- Company size: {_SIZE_FACTS.get(profile.company_size or '', 'not given')}\n"
        f"- Where the goods come from: {_SUPPLY_FACTS.get(profile.supply or '', 'not given')}"
    )


def render_change(change: RegChange) -> str:
    """Numbered obligations with their quotes; ends with `Source URL: <url>` (fixture key)."""
    dates = ", ".join(d.isoformat() for d in change.effective_dates) or "none given"
    lines = [
        "## Regulation change",
        f"- Instrument: {change.instrument}",
        f"- Jurisdiction: {change.jurisdiction}",
        f"- Summary: {change.summary}",
        f"- Effective dates: {dates}",
        "- Obligations:" if change.obligations else "- Obligations: none",
    ]
    for i, o in enumerate(change.obligations, 1):
        lines.append(f"  {i}. {o.text}")
        lines.append(f'     quote: "{o.quote}"')
    lines.append("")
    lines.append(f"{SOURCE_URL_PREFIX}{change.document_url}")
    return "\n".join(lines)


def render_date_notes(moves: Iterable[RuleMove], profile: Profile) -> str:
    """Grounded date rules of the change's act for this store ('' when none): each stored
    RuleMove for the store's size, narrowed to what it sells, with every exception's product line
    and the date those products keep. All quotes passed `RuleMove.validate_sources` when seeded;
    they only steer `due`, which `enforce()` still checks against the change's effective dates."""
    lines: list[str] = []
    for move in moves:
        if not move.for_profile(profile):
            continue
        mv = move.for_store(profile)
        lines.append(
            f"- For {mv.audience}: {mv.instrument}, {mv.provision}, as amended by "
            f'{mv.amending_act}, sets {mv.new_date.isoformat()}: "{mv.quote}"'
        )
        for ex in mv.exceptions:
            lines.append(
                f"  - Except {ex.products} (this shop sells {', '.join(ex.categories)}): "
                f'those keep {ex.due.isoformat()}. {ex.act}, {ex.provision}, lists: "{ex.quote}". '
                f'{ex.due_provision}: "{ex.due_quote}"'
            )
    if not lines:
        return ""
    return "## Date notes for this act (verbatim from stored sources)\n" + "\n".join(lines)


def build_messages(change: RegChange, profile: Profile, notes: str = "") -> list[dict[str, str]]:
    parts = [render_profile(profile), notes, render_change(change)]
    return [
        {"role": "system", "content": load_prompt()},
        {"role": "user", "content": "\n\n".join(p for p in parts if p)},
    ]


def retry_message(change: RegChange, rejected: list[TodoDraft]) -> str:
    """Second user message; an empty `rejected` list means the reply was not parseable."""
    if rejected:
        listed = "\n".join(f'- "{t.text}" — quote given: "{t.quote}"' for t in rejected)
        head = (
            f"Your previous answer had {len(rejected)} to-do(s) whose quote was not copied "
            f"exactly from the numbered obligations:\n{listed}\n"
        )
    else:
        head = "Your previous answer was not one valid JSON object matching the schema.\n"
    return (
        f"{head}"
        "Return the complete JSON object again. Keep the decision and reasons unless they were "
        "wrong. For each rejected to-do either copy the exact quote of the obligation it belongs "
        "to, or leave that to-do out.\n\n"
        f"{SOURCE_URL_PREFIX}{change.document_url}"
    )


def parse_reply[M: BaseModel](text: str, model: type[M]) -> M:
    """One `model` JSON object from a reply: strips <think>, ``` fences and prose around it (as
    normalize); raises PayloadError("invalid_json" | "schema")."""
    body = _THINK_RE.sub("", text)
    fenced = _FENCE_RE.search(body)
    if fenced:
        body = fenced.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise PayloadError("invalid_json", "no JSON object in reply")
    try:
        return model.model_validate_json(body[start : end + 1])
    except ValidationError as e:
        errors = e.errors()
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in errors
        )
        kind = "invalid_json" if any(err["type"] == "json_invalid" for err in errors) else "schema"
        raise PayloadError(kind, detail) from e


def parse_payload(text: str) -> ClassifiedPayload:
    """Same stripping as normalize (<think>, ``` fences, prose); raises PayloadError."""
    return parse_reply(text, ClassifiedPayload)


def _ref(change: RegChange) -> str:
    """Log reference: the URL plus the instrument, which carries a long act's section label."""
    return f"{change.document_url} [{change.instrument}]"


def _parse_due(raw: str | None, change: RegChange, todo_text: str) -> date | None:
    """An ISO date among the change's effective dates, else None with a WARNING (never invented)."""
    if raw is None:
        return None
    candidate = raw.strip()
    due: date | None
    try:
        if not _ISO_DATE_RE.match(candidate):
            raise ValueError("not YYYY-MM-DD")
        due = date.fromisoformat(candidate)
    except ValueError:
        due = None
    if due is None or due not in change.effective_dates:
        log.warning("classify: nulled invented due=%s url=%s todo=%r", raw, _ref(change), todo_text)
        return None
    return due


def _cap(todos: list[Todo]) -> list[Todo]:
    """To-dos of the first MAX_TODOS distinct quotes; at most two per quote (one per product
    group when the date notes give some products an earlier date)."""
    per_quote: dict[str, int] = {}
    kept: list[Todo] = []
    for t in todos:
        q = " ".join(t.quote.split())
        if q not in per_quote and len(per_quote) >= MAX_TODOS:
            continue
        if per_quote.get(q, 0) >= 2:
            continue
        per_quote[q] = per_quote.get(q, 0) + 1
        kept.append(t)
    return kept


def enforce(payload: ClassifiedPayload, change: RegChange) -> tuple[Applicability, list[Todo], int]:
    """(grounded applicability, rejected to-dos, nulled dues). The grounding rule lives here.

    Order, never relaxed: citation is ours → invented dues nulled → `does_not` keeps no to-dos and
    `unclear` at most the first grounded one → at most MAX_TODOS → ungrounded to-dos rejected.
    """
    todos: list[Todo] = []
    nulled = 0
    for draft in payload.todos:
        due = _parse_due(draft.due, change, draft.text)
        if draft.due is not None and due is None:
            nulled += 1
        todos.append(
            Todo(text=draft.text, due=due, quote=draft.quote, citation_url=change.document_url)
        )
    grounded_quotes = {" ".join(o.quote.split()) for o in change.obligations}
    if payload.decision == "does_not":
        if todos:
            log.info("classify: dropped %d to-do(s) on does_not url=%s", len(todos), _ref(change))
        todos = []
    elif payload.decision == "unclear":
        first = [t for t in todos if " ".join(t.quote.split()) in grounded_quotes][:1]
        if len(todos) > len(first):
            log.info(
                "classify: kept %d of %d to-do(s) on unclear url=%s",
                len(first),
                len(todos),
                _ref(change),
            )
        todos = first
    capped = _cap(todos)
    if len(capped) < len(todos):
        log.info(
            "classify: truncated %d to-dos to %d url=%s", len(todos), len(capped), _ref(change)
        )
        todos = capped
    proposed = Applicability(decision=payload.decision, reasons=payload.reasons, todos=todos)
    rejected = proposed.ungrounded_todos(change)
    grounded = proposed.model_copy(
        update={"todos": [t for t in proposed.todos if t not in rejected]}
    )
    return grounded, rejected, nulled


@dataclass
class ClassifyResult:
    applicability: Applicability | None
    retried: bool
    dropped_todos: list[Todo]
    nulled_dues: int
    usage: LLMUsage | None
    cost_usd: float
    model: str
    error: str | None  # invalid_json | schema | backend:<ExcName>


@dataclass
class _Attempt:
    """Bookkeeping across the (at most) two model calls of one change."""

    settings: Settings
    backend: LLMBackend
    ledger: ModelLedger
    url: str
    instrument: str = ""
    usage: LLMUsage | None = None
    cost_usd: float = 0.0
    n_calls: int = 0
    nulled: int = 0
    dropped: list[Todo] = field(default_factory=list)

    @property
    def retried(self) -> bool:
        return self.n_calls > 1

    def call(self, messages: list[dict[str, str]]) -> str:
        self.n_calls += 1
        result = self.backend.complete(
            model=self.settings.classifier_model,
            messages=messages,
            response_format=response_format(),
        )
        cost = self.ledger.add(result.usage)
        prev = self.usage or LLMUsage()
        self.usage = LLMUsage(
            prompt_tokens=prev.prompt_tokens + result.usage.prompt_tokens,
            completion_tokens=prev.completion_tokens + result.usage.completion_tokens,
        )
        self.cost_usd += cost
        log.info(
            "classify url=%s model=%s attempt=%d usage=%s cost=$%.4f instrument=%r",
            self.url,
            result.model,
            self.n_calls,
            f"{result.usage.prompt_tokens}/{result.usage.completion_tokens}",
            cost,
            self.instrument,
        )
        return result.text

    def done(self, appl: Applicability | None, *, error: str | None) -> ClassifyResult:
        if appl is None:
            log.warning("not classified (%s): %s [%s]", error, self.url, self.instrument)
        return ClassifyResult(
            applicability=appl,
            retried=self.retried,
            dropped_todos=self.dropped,
            nulled_dues=self.nulled,
            usage=self.usage,
            cost_usd=self.cost_usd,
            model=self.settings.classifier_model,
            error=error,
        )


def _parsed(reply: str) -> tuple[ClassifiedPayload | None, str | None]:
    try:
        return parse_payload(reply), None
    except PayloadError as e:
        return None, e.kind


def classify(
    change: RegChange,
    profile: Profile,
    *,
    backend: LLMBackend,
    settings: Settings,
    ledger: ModelLedger,
    moves: Iterable[RuleMove] = (),
) -> ClassifyResult:
    """One (change, profile) → grounded Applicability or a failure; one retry; never raises.
    `moves`: stored rule moves of the change's act, rendered as date notes (`render_date_notes`)."""
    messages = build_messages(change, profile, render_date_notes(moves, profile))
    attempt = _Attempt(
        settings=settings,
        backend=backend,
        ledger=ledger,
        url=change.document_url,
        instrument=change.instrument,
    )

    try:
        reply = attempt.call(messages)
    except Exception as e:
        return attempt.done(None, error=f"backend:{type(e).__name__}")
    payload, error = _parsed(reply)
    appl: Applicability | None = None
    rejected: list[Todo] = []
    if payload is not None:
        appl, rejected, attempt.nulled = enforce(payload, change)

    if payload is None or rejected:
        # Exactly one retry: a parse failure counts as "every to-do rejected".
        drafts = [TodoDraft(text=t.text, due=None, quote=t.quote) for t in rejected]
        retry = [
            *messages,
            {"role": "assistant", "content": reply},
            {"role": "user", "content": retry_message(change, drafts)},
        ]
        try:
            reply = attempt.call(retry)
        except Exception as e:
            return attempt.done(None, error=f"backend:{type(e).__name__}")
        payload, error = _parsed(reply)
        if payload is None:
            return attempt.done(None, error=error)
        appl, rejected, attempt.nulled = enforce(payload, change)

    assert appl is not None
    attempt.dropped = rejected
    for t in rejected:
        log.warning("classify: dropped ungrounded to-do %r url=%s", t.text, _ref(change))
    if appl.decision == "applies" and not appl.todos:
        log.warning("classify: applies with 0 grounded to-dos url=%s", _ref(change))
    return attempt.done(appl, error=None)


@dataclass
class ClassifySummary:
    n_changes: int
    n_applies: int
    n_does_not: int
    n_unclear: int
    n_retried: int
    n_failed: int
    n_todos: int
    n_todos_dropped: int
    cost_usd: float

    @classmethod
    def empty(cls) -> ClassifySummary:
        return cls(0, 0, 0, 0, 0, 0, 0, 0, 0.0)

    def line(self) -> str:
        return (
            f"classify: {self.n_changes} changes · {self.n_applies} apply · "
            f"{self.n_does_not} don't · {self.n_unclear} unclear · {self.n_retried} retried · "
            f"{self.n_failed} failed · {self.n_todos} to-dos ({self.n_todos_dropped} dropped) · "
            f"${self.cost_usd:.4f}"
        )


def classify_all(
    s: Session,
    *,
    profile_row: ProfileRow,
    backend: LLMBackend,
    settings: Settings,
    ledger: ModelLedger,
) -> ClassifySummary:
    """Classify every RegChangeRow without an ApplicabilityRow for this profile and store the
    grounded results. Failures store nothing and are retried on the next crawl."""
    assert profile_row.id is not None
    profile = profile_row.to_schema()
    rows = regchanges_needing_classify(s, profile_row.id)
    summary = ClassifySummary.empty()
    summary.n_changes = len(rows)
    for row in rows:
        assert row.id is not None
        change = row.to_schema()
        result = classify(
            change,
            profile,
            backend=backend,
            settings=settings,
            ledger=ledger,
            moves=instrument_moves(s, change.instrument),
        )
        summary.cost_usd += result.cost_usd
        summary.n_todos_dropped += len(result.dropped_todos)
        appl = result.applicability
        if appl is None:
            summary.n_failed += 1
            continue
        # The grounding rule, enforced in code right before anything can be stored.
        if appl.ungrounded_todos(change):
            log.error("classify: refusing to store ungrounded to-dos url=%s", _ref(change))
            summary.n_failed += 1
            continue
        if result.retried:
            summary.n_retried += 1
        if appl.decision == "applies":
            summary.n_applies += 1
        elif appl.decision == "does_not":
            summary.n_does_not += 1
        else:
            summary.n_unclear += 1
        summary.n_todos += len(appl.todos)
        store_applicability(
            s,
            profile_id=profile_row.id,
            regchange_id=row.id,
            appl=appl,
            model=settings.classifier_model,
            cost_usd=result.cost_usd,
        )
    log.info("%s", summary.line())
    return summary
