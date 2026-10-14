import json
import logging
from datetime import date
from typing import Any

import pytest
from sqlmodel import col, select

from regwatch.agent.budget import DAILY_BUDGET_USED, RUN_BUDGET_USED, Budget, Reservation
from regwatch.agent.grounding import DOWNGRADE_REASON
from regwatch.agent.loop import (
    GROUNDING_FAILED,
    NO_DECISION,
    advance,
    build_messages,
    load_agent_prompt,
    load_context,
    record_answer,
    transition,
)
from regwatch.agent.schemas import AgentOutcome
from regwatch.config import Settings
from regwatch.db import (
    AgentFactRow,
    AgentPageRow,
    AgentRunRow,
    AgentStepRow,
    session,
    utcnow,
)
from regwatch.ingest.tavily_backend import normalize_url
from regwatch.schemas import Applicability, RegChange, Todo
from regwatch.understand.classify import ClassifiedPayload
from regwatch.understand.llm_backend import LLMResult
from tests.conftest import EUDR_URL, AgentDB, eudr_change, new_run
from tests.fakes import (
    IHK_URL,
    OPTIONS,
    TRADER_QUOTE,
    ScriptedLLM,
    ScriptedTavily,
    agent_action,
    decide_does_not,
    decide_unclear,
    ihk_tavily,
    until_question,
)

CHANGE = eudr_change()
DUE_DILIGENCE = CHANGE.obligations[3].quote
CALL_USD = (2_000 * 1.00 + 150 * 3.00) / 1_000_000


def _advance(
    db: AgentDB,
    run_id: int,
    llm: Any,
    tavily: Any,
    settings: Settings | None = None,
    budget: Budget | None = None,
) -> str:
    return advance(
        run_id,
        engine=db.engine,
        settings=settings or db.settings,
        llm=llm,
        tavily=tavily,
        budget=budget or Budget(),
    )


def _run_row(db: AgentDB, run_id: int) -> AgentRunRow:
    with session(db.engine) as s:
        row = s.get(AgentRunRow, run_id)
        assert row is not None
        return row


def _steps(db: AgentDB, run_id: int) -> list[AgentStepRow]:
    with session(db.engine) as s:
        return list(
            s.exec(
                select(AgentStepRow)
                .where(AgentStepRow.run_id == run_id)
                .order_by(col(AgentStepRow.idx))
            ).all()
        )


def _section(content: str, head: str) -> str:
    return content.split(f"## {head}\n", 1)[1].split("\n\n## ", 1)[0]


def _decide_applies(
    quote: str, text: str = "Write down your suppliers", due: str | None = None
) -> str:
    return agent_action(
        "decide",
        decision="applies",
        reasons=[
            {
                "text": "You place wood products on the EU market.",
                "fact_ids": [],
                "evidence_ids": [],
                "obligation_numbers": [4],
            }
        ],
        todos=[{"text": text, "due": due, "quote": quote}],
        evidence=[],
    )


def _nothing_held(db: AgentDB, budget: Budget) -> None:
    """Every reservation was released: what is left today is the limit minus settled steps."""
    with session(db.engine) as s:
        steps = s.exec(select(AgentStepRow)).all()
        usd = sum(st.cost_usd for st in steps)
        credits = sum(st.credits for st in steps)
    left_usd, left_credits = budget.day_left(db.engine, db.settings, utcnow())
    assert left_usd == pytest.approx(db.settings.agent_daily_usd - usd)
    assert left_credits == db.settings.agent_daily_credits - credits


def test_full_run_waits_for_the_merchant_then_decides(agent_db: AgentDB) -> None:
    llm, tavily = ScriptedLLM(until_question()), ihk_tavily()
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, tavily) == "waiting"
    assert [s.tool for s in _steps(agent_db, run_id)] == [
        "search_sources",
        "read_source",
        "ask_merchant",
    ]
    with session(agent_db.engine) as s:
        (fact,) = s.exec(select(AgentFactRow)).all()
    assert fact.chosen_index is None and json.loads(fact.options_json) == OPTIONS
    assert record_answer(agent_db.engine, run_id, 1) == "ok"
    assert _run_row(agent_db, run_id).status == "queued"
    llm.replies.append(decide_does_not())
    assert _advance(agent_db, run_id, llm, tavily) == "done"
    run = _run_row(agent_db, run_id)
    assert (run.status, run.decision, run.n_steps, run.tavily_credits) == ("done", "does_not", 4, 2)
    assert run.cost_usd == pytest.approx(4 * CALL_USD)
    outcome = AgentOutcome.model_validate_json(run.outcome_json)
    assert outcome.reasons[0].fact_ids == [1] and outcome.reasons[0].evidence_ids == [1]
    assert outcome.facts[0].answer == OPTIONS[1] and outcome.applicability.todos == []
    assert tavily.search_calls[0]["search_depth"] == "basic"
    assert tavily.extract_calls[0]["extract_depth"] == "basic"


def test_replay_after_answer_rebuilds_the_same_context(agent_db: AgentDB) -> None:
    llm, tavily = ScriptedLLM(until_question()), ihk_tavily()
    run_id = new_run(agent_db)
    _advance(agent_db, run_id, llm, tavily)
    waiting = build_messages(load_context(agent_db.engine, run_id), agent_db.settings)
    assert waiting == build_messages(load_context(agent_db.engine, run_id), agent_db.settings)
    record_answer(agent_db.engine, run_id, 1)
    expected = build_messages(load_context(agent_db.engine, run_id), agent_db.settings)
    llm.replies.append(decide_does_not())
    _advance(agent_db, run_id, llm, tavily)
    assert llm.calls[3] == expected
    assert llm.calls[3][0] == llm.calls[0][0]
    before = _section(llm.calls[2][1]["content"], "Steps so far")
    after = _section(expected[1]["content"], "Steps so far")
    assert after.startswith(before)
    assert f'merchant answered: fact #1: "{OPTIONS[1]}"' in after
    assert "fact #1:" in _section(expected[1]["content"], "Facts the merchant gave")


def test_second_advance_runs_nothing(agent_db: AgentDB) -> None:
    llm, tavily = ScriptedLLM(until_question()), ihk_tavily()
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, tavily) == "waiting"
    n = len(llm.calls)
    assert _advance(agent_db, run_id, llm, tavily) == "" and len(llm.calls) == n
    assert transition(agent_db.engine, run_id, from_=("queued",), to="running") is False
    assert record_answer(agent_db.engine, run_id, 1) == "ok"
    assert transition(agent_db.engine, run_id, from_=("queued",), to="running") is True
    assert transition(agent_db.engine, run_id, from_=("queued",), to="running") is False


def test_record_answer_accepts_option_indices_only(agent_db: AgentDB) -> None:
    run_id = new_run(agent_db)
    _advance(agent_db, run_id, ScriptedLLM(until_question()), ihk_tavily())
    assert record_answer(agent_db.engine, run_id, 2) == "bad_option"
    assert record_answer(agent_db.engine, run_id, -1) == "bad_option"
    assert _run_row(agent_db, run_id).status == "waiting"
    assert record_answer(agent_db.engine, run_id, 0) == "ok"
    assert record_answer(agent_db.engine, run_id, 0) == "not_waiting"


def test_invalid_json_counts_as_a_step(agent_db: AgentDB) -> None:
    llm = ScriptedLLM(["I think it applies.", decide_unclear()])
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, ihk_tavily()) == "done"
    steps = _steps(agent_db, run_id)
    assert [s.tool for s in steps] == ["invalid", "decide"]
    assert "not one JSON object" in json.loads(steps[0].result_json)["error"]
    assert "not one JSON object" in llm.calls[1][1]["content"]
    assert _run_row(agent_db, run_id).n_steps == 2


def test_refused_tools_count_as_steps_and_cost_no_credits(agent_db: AgentDB) -> None:
    llm = ScriptedLLM(
        [
            agent_action("search_sources", query="EUDR micro", domain="example.com"),
            agent_action("read_source", url=IHK_URL),  # never shown in this run
            decide_unclear(),
        ]
    )
    tavily = ihk_tavily()
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, tavily) == "done"
    assert tavily.search_calls == [] and tavily.extract_calls == []
    steps = _steps(agent_db, run_id)
    assert all("refused" in json.loads(s.result_json) for s in steps[:2])
    run = _run_row(agent_db, run_id)
    assert run.tavily_credits == 0 and run.n_steps == 3


def test_change_document_can_be_read_without_a_search(agent_db: AgentDB) -> None:
    tavily = ScriptedTavily(
        pages={EUDR_URL: "# Regulation (EU) 2023/1115\n\nOperators shall exercise due diligence."}
    )
    llm = ScriptedLLM([agent_action("read_source", url=EUDR_URL), decide_unclear()])
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, tavily) == "done"
    with session(agent_db.engine) as s:
        (page,) = s.exec(select(AgentPageRow)).all()
    assert page.url == normalize_url(EUDR_URL) and page.run_id == run_id
    result = json.loads(_steps(agent_db, run_id)[0].result_json)
    assert result["page_id"] == page.id and result["head"].startswith("# Regulation")


def test_last_step_is_forced_to_decide(agent_db: AgentDB) -> None:
    settings = agent_db.settings.model_copy(update={"agent_run_steps": 2})
    llm = ScriptedLLM([agent_action("search_sources", query="EUDR KMU", domain="ihk.de")] * 2)
    tavily = ihk_tavily()
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, tavily, settings) == "stopped"
    first: Any = llm.formats[0]
    last: Any = llm.formats[1]
    assert len(first["json_schema"]["schema"]["properties"]["tool"]["enum"]) == 4
    assert last["json_schema"]["schema"]["properties"]["tool"]["enum"] == ["decide"]
    assert "It is your last step" in llm.calls[1][1]["content"]
    assert len(tavily.search_calls) == 1
    run = _run_row(agent_db, run_id)
    assert run.stop_reason == NO_DECISION and run.n_steps == 2


def test_decide_retries_once_for_rejected_todos(agent_db: AgentDB) -> None:
    llm = ScriptedLLM(
        [
            _decide_applies("Keep a list of suppliers."),
            _decide_applies(DUE_DILIGENCE, "Check each product before you sell it", "2026-12-30"),
        ]
    )
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, ihk_tavily()) == "done"
    first = json.loads(_steps(agent_db, run_id)[0].result_json)
    assert first["error"] == "rejected_todos" and "Keep a list of suppliers." in first["retry"]
    assert "whose quote was not copied exactly" in llm.calls[1][1]["content"]
    outcome = AgentOutcome.model_validate_json(_run_row(agent_db, run_id).outcome_json)
    assert outcome.applicability.todos == [
        Todo(
            text="Check each product before you sell it",
            due=date(2026, 12, 30),
            quote=DUE_DILIGENCE,
            citation_url=CHANGE.document_url,
        )
    ]


def test_decide_retry_only_once(agent_db: AgentDB) -> None:
    llm = ScriptedLLM([_decide_applies("Keep a list of suppliers.")] * 2)
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, ihk_tavily()) == "done"
    run = _run_row(agent_db, run_id)
    outcome = AgentOutcome.model_validate_json(run.outcome_json)
    assert run.n_steps == 2 and outcome.applicability.todos == []
    assert outcome.applicability.decision == "applies"
    assert any("Write down your suppliers" in d for d in outcome.dropped)


def test_no_retry_on_the_last_step(agent_db: AgentDB) -> None:
    settings = agent_db.settings.model_copy(update={"agent_run_steps": 1})
    run_id = new_run(agent_db)
    llm = ScriptedLLM([_decide_applies("Keep a list of suppliers.")])
    assert _advance(agent_db, run_id, llm, ihk_tavily(), settings) == "done"
    run = _run_row(agent_db, run_id)
    assert run.n_steps == 1
    assert AgentOutcome.model_validate_json(run.outcome_json).applicability.todos == []


def test_leaked_ungrounded_todo_is_never_stored(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The store-boundary re-check in the loop is its own barrier, as in classify_all."""

    def leaky_enforce(
        payload: ClassifiedPayload, change: RegChange
    ) -> tuple[Applicability, list[Todo], int]:
        leaked = Todo(text="Do it", quote="NOT IN THE SOURCE", citation_url=change.document_url)
        return Applicability(decision="applies", reasons=["x"], todos=[leaked]), [], 0

    monkeypatch.setattr("regwatch.agent.grounding.enforce", leaky_enforce)
    run_id = new_run(agent_db)
    llm = ScriptedLLM([_decide_applies(DUE_DILIGENCE)])
    with caplog.at_level(logging.ERROR, logger="regwatch.agent.loop"):
        assert _advance(agent_db, run_id, llm, ihk_tavily()) == "failed"
    run = _run_row(agent_db, run_id)
    assert run.stop_reason == GROUNDING_FAILED and run.outcome_json == "" and run.decision is None
    assert "refusing to store ungrounded to-dos" in caplog.text


def test_pages_and_facts_are_scoped_to_their_run(agent_db: AgentDB) -> None:
    """Run A reads the IHK page (page #1) AND gets an answer (fact #1) before run B cites both;
    so either half of B's downgrade fails if pages or facts leaked between runs."""
    tavily = ihk_tavily()
    a = new_run(agent_db, "visitor-a")
    llm_a = ScriptedLLM(until_question())
    assert _advance(agent_db, a, llm_a, tavily) == "waiting"
    assert record_answer(agent_db.engine, a, 1) == "ok"
    llm_a.replies.append(decide_does_not())
    assert _advance(agent_db, a, llm_a, tavily) == "done"
    a_outcome = AgentOutcome.model_validate_json(_run_row(agent_db, a).outcome_json)
    assert a_outcome.reasons[0].fact_ids == [1] and a_outcome.reasons[0].evidence_ids == [1]

    b = new_run(agent_db, "visitor-b")
    cross = agent_action(
        "decide",
        decision="applies",
        reasons=[
            {
                "text": "The act binds you.",
                "fact_ids": [1],
                "evidence_ids": [1],
                "obligation_numbers": [],
            }
        ],
        todos=[],
        evidence=[{"evidence_id": 1, "page_id": 1, "quote": TRADER_QUOTE}],
    )
    llm_b = ScriptedLLM([cross])
    assert _advance(agent_db, b, llm_b, tavily) == "done"
    outcome = AgentOutcome.model_validate_json(_run_row(agent_db, b).outcome_json)
    assert outcome.applicability.decision == "unclear"
    assert outcome.applicability.reasons == [DOWNGRADE_REASON]
    assert outcome.facts == [] and outcome.evidence == []
    assert any("page #1 was not read in this run" in d for d in outcome.dropped)
    user = llm_b.calls[0][1]["content"]
    assert "fact #1" not in user and TRADER_QUOTE not in user
    ctx = load_context(agent_db.engine, b)
    assert ctx.pages == {} and ctx.facts == []


def test_daily_budget_stops_before_any_model_call(agent_db: AgentDB) -> None:
    settings = agent_db.settings.model_copy(update={"agent_daily_usd": 0.0})
    llm = ScriptedLLM([decide_unclear()])
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, ihk_tavily(), settings) == "stopped"
    run = _run_row(agent_db, run_id)
    assert llm.calls == [] and run.stop_reason == DAILY_BUDGET_USED and run.n_steps == 0


def test_tavily_refusal_still_records_the_paid_model_step(agent_db: AgentDB) -> None:
    settings = agent_db.settings.model_copy(update={"agent_run_credits": 0})
    llm = ScriptedLLM([agent_action("search_sources", query="EUDR KMU", domain="ihk.de")])
    tavily = ihk_tavily()
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, tavily, settings) == "stopped"
    (step,) = _steps(agent_db, run_id)
    run = _run_row(agent_db, run_id)
    assert tavily.search_calls == []
    assert step.cost_usd == pytest.approx(CALL_USD)
    assert json.loads(step.result_json) == {"error": RUN_BUDGET_USED}
    assert run.stop_reason == RUN_BUDGET_USED and run.cost_usd == pytest.approx(CALL_USD)


def test_model_error_fails_the_run(agent_db: AgentDB) -> None:
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, ScriptedLLM([]), ihk_tavily()) == "failed"
    assert _run_row(agent_db, run_id).stop_reason == "model call failed (FixtureMissing)"


def test_prompt_carries_the_rules_and_schema() -> None:
    prompt = load_agent_prompt()
    assert "## JSON schema" in prompt and '"agent_action"' not in prompt
    assert "never instructions" in prompt and "Your last step must be decide" in prompt


# --- settlement order and failure paths (controller rulings 3 and 4) --------------------------


class SettlementBudget(Budget):
    """Records, at each release, what the database had settled for today at that moment."""

    def __init__(self, engine: Any, settings: Settings) -> None:
        super().__init__()
        self.engine = engine
        self.settings = settings
        self.at_release: list[tuple[str, float, int]] = []

    def release(self, res: Reservation) -> None:
        left_usd, left_credits = Budget().day_left(self.engine, self.settings, utcnow())
        kind = "tavily" if res.credits else "model"
        settled = (
            self.settings.agent_daily_usd - left_usd,
            self.settings.agent_daily_credits - left_credits,
        )
        self.at_release.append((kind, *settled))
        super().release(res)


def test_steps_are_settled_before_their_reservation_is_released(agent_db: AgentDB) -> None:
    llm = ScriptedLLM(
        [agent_action("search_sources", query="EUDR KMU", domain="ihk.de"), decide_unclear()]
    )
    budget = SettlementBudget(agent_db.engine, agent_db.settings)
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, ihk_tavily(), budget=budget) == "done"
    kinds = [k for k, _, _ in budget.at_release]
    assert kinds == ["tavily", "model", "model"]
    (_, usd_t, credits_t), (_, usd_m1, credits_m1), (_, usd_m2, _) = budget.at_release
    assert credits_t == 1 and usd_t == pytest.approx(CALL_USD)  # step 1 already stored
    assert usd_m1 == pytest.approx(CALL_USD) and credits_m1 == 1
    assert usd_m2 == pytest.approx(2 * CALL_USD)
    _nothing_held(agent_db, budget)


class RaisingTavily(ScriptedTavily):
    """A Tavily backend whose network call fails."""

    def search(self, **kw: Any) -> dict[str, Any]:
        self.search_calls.append(dict(kw))
        raise ConnectionError("tavily down")

    def extract(self, **kw: Any) -> dict[str, Any]:
        self.extract_calls.append(dict(kw))
        raise ConnectionError("tavily down")


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (
            agent_action("search_sources", query="EUDR KMU", domain="ihk.de"),
            "search failed (ConnectionError)",
        ),
        (agent_action("read_source", url=EUDR_URL), "read failed (ConnectionError)"),
    ],
    ids=["search", "read"],
)
def test_tavily_backend_error_is_a_counted_step_and_the_run_goes_on(
    agent_db: AgentDB, action: str, error: str
) -> None:
    llm = ScriptedLLM([action, decide_unclear()])
    tavily = RaisingTavily()
    budget = Budget()
    run_id = new_run(agent_db)
    assert _advance(agent_db, run_id, llm, tavily, budget=budget) == "done"
    first, second = _steps(agent_db, run_id)
    assert json.loads(first.result_json) == {"error": error}
    assert first.credits == 1 and first.cost_usd == pytest.approx(CALL_USD)
    assert error in llm.calls[1][1]["content"]  # the model saw the error
    run = _run_row(agent_db, run_id)
    assert (run.n_steps, run.tavily_credits) == (2, 1)
    assert run.cost_usd == pytest.approx(2 * CALL_USD)
    _nothing_held(agent_db, budget)


class RaisingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, *, model: str, messages: list[dict[str, str]], response_format: dict[str, Any] | None
    ) -> LLMResult:
        self.calls += 1
        raise ConnectionError("token factory down")


def test_model_backend_error_fails_the_run_and_releases_its_reservation(
    agent_db: AgentDB,
) -> None:
    budget = Budget()
    run_id = new_run(agent_db)
    llm = RaisingLLM()
    assert _advance(agent_db, run_id, llm, ihk_tavily(), budget=budget) == "failed"
    run = _run_row(agent_db, run_id)
    assert llm.calls == 1
    assert (run.status, run.stop_reason) == ("failed", "model call failed (ConnectionError)")
    assert run.n_steps == 0 and _steps(agent_db, run_id) == []  # nothing was charged
    _nothing_held(agent_db, budget)


def test_crash_after_a_paid_model_call_records_the_step_and_fails(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error while acting still stores the paid model step; no half-recorded run."""

    def broken_view(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("bug")

    monkeypatch.setattr("regwatch.agent.loop.page_view", broken_view)
    tavily = ScriptedTavily(pages={EUDR_URL: "# 2023/1115\n\nOperators shall exercise care."})
    budget = Budget()
    run_id = new_run(agent_db)
    llm = ScriptedLLM([agent_action("read_source", url=EUDR_URL)])
    assert _advance(agent_db, run_id, llm, tavily, budget=budget) == "failed"
    (step,) = _steps(agent_db, run_id)
    run = _run_row(agent_db, run_id)
    assert (run.status, run.stop_reason, run.n_steps) == ("failed", "internal error", 1)
    assert step.cost_usd == pytest.approx(CALL_USD) and step.credits == 1
    assert run.cost_usd == pytest.approx(CALL_USD) and run.tavily_credits == 1
    _nothing_held(agent_db, budget)


def test_failed_step_record_after_a_paid_search_still_settles_its_credits(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paid search's step write fails once (DB error): the credits are still settled, before
    the Tavily reservation is released, and the run fails cleanly."""
    from regwatch.agent import loop

    real_record = loop._record
    calls: list[int] = []

    def flaky_record(engine: Any, run_id: int, draft: Any, **kw: Any) -> None:
        calls.append(draft.credits)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        real_record(engine, run_id, draft, **kw)

    monkeypatch.setattr(loop, "_record", flaky_record)
    budget = SettlementBudget(agent_db.engine, agent_db.settings)
    run_id = new_run(agent_db)
    llm = ScriptedLLM([agent_action("search_sources", query="EUDR KMU", domain="ihk.de")])
    assert _advance(agent_db, run_id, llm, ihk_tavily(), budget=budget) == "failed"
    assert calls == [1, 1]  # the retry in _fail_paid carries the paid credit
    (step,) = _steps(agent_db, run_id)
    run = _run_row(agent_db, run_id)
    assert (run.status, run.stop_reason) == ("failed", "internal error")
    assert step.credits == 1 and run.tavily_credits == 1
    assert step.cost_usd == pytest.approx(CALL_USD)
    (_, _, credits_at_tavily_release), _ = budget.at_release
    assert credits_at_tavily_release == 1  # settled before the release
    _nothing_held(agent_db, budget)
