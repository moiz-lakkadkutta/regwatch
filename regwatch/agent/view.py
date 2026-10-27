"""What a change page shows of one agent run (spec §3): one line per step, the open question,
the grounded result with its citations, and the run and day cost lines. Built from stored rows
only; templates autoescape every string and nothing here is marked safe."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.engine import Engine
from sqlmodel import col, select

from regwatch.agent.budget import BUDGET, NO_BUDGET, NO_TOTAL_BUDGET, Budget, worst_case_usd
from regwatch.agent.loop import THOUGHT_CHARS
from regwatch.agent.schemas import AgentOutcome
from regwatch.config import Settings
from regwatch.db import AgentFactRow, AgentRunRow, AgentStepRow, session
from regwatch.schemas import RegChange, Todo

POLLING: tuple[str, ...] = ("queued", "running")
DECISION_LABELS: dict[str, str] = {
    "applies": "applies",
    "does_not": "doesn't apply",
    "unclear": "still unclear",
}
# Steps that only redo work (an unreadable reply, a to-do quote sent back, a failed tool call)
# are counted in the run line, not listed: the list reads as what the agent found out.
REDONE_KINDS: tuple[str, ...] = ("retry", "error")


def clip_thought(text: str) -> str:
    """A thought stored at the THOUGHT_CHARS cap was cut mid-word; end it on a word, with '…'."""
    if len(text) < THOUGHT_CHARS:
        return text.strip()
    head = text.rsplit(" ", 1)[0] if " " in text.strip() else text
    return head.rstrip(" ,;:—–-") + "…"


def short_url(url: str) -> str:
    """'eur-lex.europa.eu/…' for a page, the bare host for a site root."""
    parts = urlsplit(url)
    host = parts.hostname or url
    return f"{host}/…" if parts.path.strip("/") else host


@dataclass(frozen=True)
class StepView:
    idx: int
    kind: str  # search | read | ask | decide | retry | refused | error
    thought: str
    domain: str = ""
    query: str = ""
    n_results: int = 0
    where: str = ""
    url: str = ""
    question: str = ""
    answer: str = ""
    decision: str = ""
    note: str = ""
    credits: int = 0


@dataclass(frozen=True)
class Citation:
    kind: str  # fact | quote
    text: str
    url: str = ""
    label: str = ""


@dataclass(frozen=True)
class ReasonView:
    text: str
    citations: list[Citation]


@dataclass(frozen=True)
class AgentTodo:
    todo: Todo
    in_force: bool  # due before today: "in force since", as on the rest of the site


@dataclass(frozen=True)
class ResultView:
    decision: str
    label: str
    todos: list[AgentTodo]
    reasons: list[ReasonView]
    source_domain: str


@dataclass(frozen=True)
class QuestionView:
    question: str
    options: list[str]


@dataclass(frozen=True)
class AgentPanel:
    change_id: int
    can_start: bool  # today's and the total budget allow a new run ("Try again")
    today_credits: int
    today_usd: float
    credits_cap: int
    usd_cap: float
    run_id: int | None = None
    status: str = ""  # "" = no run yet
    stop_reason: str = ""
    steps: list[StepView] = field(default_factory=list)  # without the REDONE_KINDS
    n_redone: int = 0
    question: QuestionView | None = None
    result: ResultView | None = None
    n_steps: int = 0
    credits: int = 0
    cost_usd: float = 0.0
    polling: bool = False
    message: str = ""  # a start refusal, or NO_BUDGET when a new run cannot start
    notice: str = ""  # a refused answer ("This question has closed."), shown on any status


def step_view(step: AgentStepRow, fact: AgentFactRow | None) -> StepView:
    args: dict[str, Any] = json.loads(step.args_json)
    result: dict[str, Any] = json.loads(step.result_json)
    thought = clip_thought(step.thought)
    if step.tool == "invalid":
        return StepView(step.idx, "error", thought)
    if "refused" in result:
        return StepView(step.idx, "refused", thought, note=str(result["refused"]))
    if result.get("error") == "rejected_todos":
        return StepView(step.idx, "retry", thought)
    if "error" in result:
        return StepView(step.idx, "error", thought, note=str(result["error"]), credits=step.credits)
    if step.tool == "search_sources":
        hits: list[Any] = result.get("hits", [])
        return StepView(
            step.idx,
            "search",
            thought,
            domain=str(args.get("domain", "")),
            query=str(args.get("query", "")),
            n_results=len(hits),
            credits=step.credits,
        )
    if step.tool == "read_source":
        url = str(result.get("url") or args.get("url", ""))
        return StepView(
            step.idx, "read", thought, where=short_url(url), url=url, credits=step.credits
        )
    if step.tool == "ask_merchant":
        answer = fact.chosen_text if fact is not None and fact.chosen_index is not None else ""
        return StepView(
            step.idx, "ask", thought, question=str(result.get("asked", "")), answer=answer
        )
    decision = str(result.get("decision", ""))
    return StepView(step.idx, "decide", thought, decision=DECISION_LABELS.get(decision, decision))


def result_view(
    outcome: AgentOutcome, change: RegChange, source_domain: str, today: date
) -> ResultView:
    facts = {f.fact_id: f for f in outcome.facts}
    evidence = {e.evidence_id: e for e in outcome.evidence}
    reasons: list[ReasonView] = []
    for r in outcome.reasons:
        cites = [
            Citation("fact", f"You told us: {facts[i].answer}") for i in r.fact_ids if i in facts
        ]
        cites += [
            Citation("quote", evidence[i].quote, evidence[i].url, short_url(evidence[i].url))
            for i in r.evidence_ids
            if i in evidence
        ]
        cites += [
            Citation("quote", change.obligations[n - 1].quote, change.document_url, source_domain)
            for n in r.obligation_numbers
            if 1 <= n <= len(change.obligations)
        ]
        reasons.append(ReasonView(r.text, cites))
    appl = outcome.applicability
    return ResultView(
        decision=appl.decision,
        label=DECISION_LABELS[appl.decision],
        todos=[AgentTodo(t, bool(t.due and t.due < today)) for t in appl.todos],
        reasons=reasons,
        source_domain=source_domain,
    )


def build_panel(
    engine: Engine,
    settings: Settings,
    *,
    change_id: int,
    change: RegChange,
    source_domain: str,
    run: AgentRunRow | None,
    now: datetime,
    message: str = "",
    budget: Budget = BUDGET,
) -> AgentPanel:
    usd_left, credits_left = budget.day_left(engine, settings, now)
    total_usd_left, total_credits_left = budget.total_left(engine, settings)
    total_ok = total_usd_left >= worst_case_usd(settings, 0) and total_credits_left >= 1
    can_start = total_ok and usd_left >= worst_case_usd(settings, 0) and credits_left >= 1
    base = AgentPanel(
        change_id=change_id,
        can_start=can_start,
        today_credits=max(0, settings.agent_daily_credits - credits_left),
        today_usd=max(0.0, settings.agent_daily_usd - usd_left),
        credits_cap=settings.agent_daily_credits,
        usd_cap=settings.agent_daily_usd,
        # said once: a start refused for budget already carries the refusal as its message
        message=message or ("" if can_start else NO_BUDGET if total_ok else NO_TOTAL_BUDGET),
    )
    if run is None or run.id is None:
        return base
    with session(engine) as s:
        steps = list(
            s.exec(
                select(AgentStepRow)
                .where(AgentStepRow.run_id == run.id)
                .order_by(col(AgentStepRow.idx))
            ).all()
        )
        facts = list(
            s.exec(
                select(AgentFactRow)
                .where(AgentFactRow.run_id == run.id)
                .order_by(col(AgentFactRow.id))
            ).all()
        )
    by_step = {f.step_idx: f for f in facts}
    open_fact = next((f for f in facts if f.chosen_index is None), None)
    question = None
    if run.status == "waiting" and open_fact is not None:
        question = QuestionView(
            question=open_fact.question, options=json.loads(open_fact.options_json)
        )
    result = None
    if run.status == "done" and run.outcome_json:
        result = result_view(
            AgentOutcome.model_validate_json(run.outcome_json), change, source_domain, now.date()
        )
    views = [step_view(st, by_step.get(st.idx)) for st in steps]
    return replace(
        base,
        run_id=run.id,
        status=run.status,
        stop_reason=run.stop_reason,
        steps=[v for v in views if v.kind not in REDONE_KINDS],
        n_redone=sum(v.kind in REDONE_KINDS for v in views),
        question=question,
        result=result,
        n_steps=run.n_steps,
        credits=run.tavily_credits,
        cost_usd=run.cost_usd,
        polling=run.status in POLLING,
    )
