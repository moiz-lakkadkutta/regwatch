"""Live smoke test for the agent on the seed's EUDR `unclear` change (Nemotron 3 Ultra + Tavily
basic). Manual only: `uv run pytest -m live -s tests/test_agent_live.py`; skipped without both
keys. Capped in Settings at 4 steps, $0.10 per day and 10 Tavily credits; prints the numbers
FEEDBACK.md needs."""

import json
import time
from pathlib import Path

import pytest
from sqlmodel import col, select

from regwatch.agent.loop import advance, record_answer
from regwatch.agent.runner import live_backends
from regwatch.agent.schemas import AgentOutcome
from regwatch.config import Settings
from regwatch.db import (
    AgentFactRow,
    AgentRunRow,
    AgentStepRow,
    ApplicabilityRow,
    DocumentRow,
    RegChangeRow,
    make_engine,
    session,
)
from regwatch.jobs.crawl import seed
from tests.conftest import EUDR_UNCLEAR, seed_change_id


@pytest.mark.live
def test_agent_on_the_eudr_unclear_change_live(tmp_path: Path) -> None:
    settings = Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'live.db'}").model_copy(
        update={
            "agent_enabled": True,
            "agent_run_steps": 4,
            "agent_daily_usd": 0.10,
            "agent_daily_credits": 10,
            "agent_run_credits": 10,
        }
    )
    if not settings.nebius_api_key or not settings.tavily_api_key:
        pytest.skip("NEBIUS_API_KEY / TAVILY_API_KEY empty")
    seed(settings)
    engine = make_engine(settings)
    with session(engine) as s:
        docs_before = sorted((d.url, d.content_hash) for d in s.exec(select(DocumentRow)).all())
        n_appl = len(s.exec(select(ApplicabilityRow)).all())
        row = AgentRunRow(
            visitor_id="live-test", profile_id=1, regchange_id=seed_change_id(EUDR_UNCLEAR)
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.id is not None
        run_id = row.id
    backends = live_backends(settings)
    kw = {"engine": engine, "settings": settings, "llm": backends.llm, "tavily": backends.tavily}
    started = time.monotonic()
    status = advance(run_id, **kw)
    for _ in range(2):
        if status != "waiting":
            break
        with session(engine) as s:
            fact = s.exec(
                select(AgentFactRow)
                .where(AgentFactRow.run_id == run_id)
                .order_by(col(AgentFactRow.id).desc())
            ).first()
        assert fact is not None
        print(f"\nasked: {fact.question} options={fact.options_json} -> answering 0")
        assert record_answer(engine, run_id, 0) == "ok"
        status = advance(run_id, **kw)
    elapsed = time.monotonic() - started
    with session(engine) as s:
        run = s.get(AgentRunRow, run_id)
        assert run is not None
        steps = s.exec(
            select(AgentStepRow)
            .where(AgentStepRow.run_id == run_id)
            .order_by(col(AgentStepRow.idx))
        ).all()
        for st in steps:
            print(
                f"step {st.idx} {st.tool} credits={st.credits} "
                f"tokens={st.prompt_tokens}/{st.completion_tokens} "
                f"cost=${st.cost_usd:.4f} thought={st.thought!r}"
            )
        print(
            f"live agent: status={run.status} stop={run.stop_reason!r} steps={run.n_steps} "
            f"credits={run.tavily_credits} cost_usd={run.cost_usd:.4f} "
            f"latency_per_step_s={elapsed / max(1, run.n_steps):.1f}"
        )
        assert run.cost_usd <= 0.10 and run.tavily_credits <= 10
        assert sorted((d.url, d.content_hash) for d in s.exec(select(DocumentRow)).all()) == (
            docs_before
        )
        assert len(s.exec(select(ApplicabilityRow)).all()) == n_appl
        assert run.status == "done"
        assert run.outcome_json is not None
        outcome = AgentOutcome.model_validate_json(run.outcome_json)
        print(json.dumps(outcome.model_dump(mode="json"), indent=2, ensure_ascii=False))
        change_row = s.get(RegChangeRow, seed_change_id(EUDR_UNCLEAR))
        assert change_row is not None
        assert outcome.applicability.ungrounded_todos(change_row.to_schema()) == []
    assert status == "done"
