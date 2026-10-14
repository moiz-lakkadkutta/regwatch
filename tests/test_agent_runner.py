import importlib
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from regwatch.agent import runner
from regwatch.agent.budget import Budget
from regwatch.agent.loop import advance
from regwatch.agent.runner import (
    EXPIRED,
    INTERRUPTED,
    UNAVAILABLE,
    Backends,
    expire_if_stale,
    start_run,
    sweep_interrupted,
)
from regwatch.config import Settings
from regwatch.db import AgentRunRow, make_engine, session, utcnow
from tests.conftest import AgentDB, new_run
from tests.fakes import ScriptedLLM, ScriptedTavily, decide_unclear, ihk_tavily, until_question


def _set(db: AgentDB, run_id: int, **fields: Any) -> None:
    with session(db.engine) as s:
        row = s.get(AgentRunRow, run_id)
        assert row is not None
        for key, value in fields.items():
            setattr(row, key, value)
        s.add(row)
        s.commit()


def _run(db: AgentDB, run_id: int) -> AgentRunRow:
    with session(db.engine) as s:
        row = s.get(AgentRunRow, run_id)
        assert row is not None
        return row


def _all_runs(db: AgentDB) -> list[AgentRunRow]:
    with session(db.engine) as s:
        return list(s.exec(select(AgentRunRow)).all())


def test_executor_runs_two_at_once() -> None:
    assert runner._EXECUTOR._max_workers == 2


def test_submit_runs_advance_off_the_request_thread(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLLM([decide_unclear()])
    seen: list[str] = []

    def backends(settings: Settings) -> Backends:
        seen.append(threading.current_thread().name)
        return Backends(llm=llm, tavily=ScriptedTavily())

    monkeypatch.setattr(runner, "make_backends", backends)
    run_id = new_run(agent_db)
    runner.submit(run_id, engine=agent_db.engine, settings=agent_db.settings).result(timeout=10)
    assert _run(agent_db, run_id).status == "done"
    assert seen[0].startswith("regwatch-agent")


def test_double_submit_runs_once(agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ScriptedLLM([decide_unclear()])
    monkeypatch.setattr(
        runner, "make_backends", lambda settings: Backends(llm=llm, tavily=ScriptedTavily())
    )
    run_id = new_run(agent_db)
    futures = [
        runner.submit(run_id, engine=agent_db.engine, settings=agent_db.settings) for _ in range(2)
    ]
    for f in futures:
        f.result(timeout=10)
    assert len(llm.calls) == 1 and _run(agent_db, run_id).status == "done"


def test_missing_keys_fail_the_run_without_network(agent_db: AgentDB) -> None:
    settings = agent_db.settings.model_copy(update={"nebius_api_key": "", "tavily_api_key": ""})
    run_id = new_run(agent_db)
    runner.run_now(run_id, engine=agent_db.engine, settings=settings)
    run = _run(agent_db, run_id)
    assert (run.status, run.stop_reason) == ("failed", UNAVAILABLE)


def test_sweep_marks_interrupted_runs_failed(agent_db: AgentDB) -> None:
    ids = {status: new_run(agent_db) for status in ("queued", "running", "waiting", "done")}
    for status, run_id in ids.items():
        _set(agent_db, run_id, status=status)
    assert sweep_interrupted(agent_db.engine) == 2
    assert _run(agent_db, ids["queued"]).stop_reason == INTERRUPTED
    assert [_run(agent_db, ids[s]).status for s in ("queued", "running", "waiting", "done")] == [
        "failed",
        "failed",
        "waiting",
        "done",
    ]


def test_app_startup_sweeps_interrupted_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'boot.db'}"
    engine = make_engine(Settings(REGWATCH_DB_URL=url))
    with session(engine) as s:
        s.add(AgentRunRow(visitor_id="v", profile_id=1, regchange_id=14, status="running"))
        s.commit()
    monkeypatch.setenv("REGWATCH_DB_URL", url)
    import regwatch.app as appmod

    importlib.reload(appmod)
    with session(engine) as s:
        (run,) = s.exec(select(AgentRunRow)).all()
    assert (run.status, run.stop_reason) == ("failed", INTERRUPTED)


def test_waiting_expires_after_30_minutes(agent_db: AgentDB) -> None:
    now = utcnow()
    old, fresh, done = new_run(agent_db), new_run(agent_db), new_run(agent_db)
    _set(agent_db, old, status="waiting", updated_at=now - timedelta(minutes=31))
    _set(agent_db, fresh, status="waiting", updated_at=now - timedelta(minutes=29))
    _set(agent_db, done, status="done", updated_at=now - timedelta(hours=2))
    assert expire_if_stale(agent_db.engine, old, now=now) is True
    assert expire_if_stale(agent_db.engine, fresh, now=now) is False
    assert expire_if_stale(agent_db.engine, done, now=now) is False
    assert (_run(agent_db, old).status, _run(agent_db, old).stop_reason) == ("expired", EXPIRED)


def test_start_run_reuses_the_active_run_and_submits_new_ones(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[int] = []
    monkeypatch.setattr(
        runner, "submit", lambda run_id, *, engine, settings: submitted.append(run_id)
    )
    kw: dict[str, Any] = {
        "visitor_id": "v",
        "profile_id": 1,
        "regchange_id": agent_db.change_id,
        "now": utcnow(),
    }
    first, refusal = start_run(agent_db.engine, agent_db.settings, **kw)
    assert first is not None and refusal == "" and submitted == [first]
    again, _ = start_run(agent_db.engine, agent_db.settings, **kw)
    assert again == first and submitted == [first]
    assert len(_all_runs(agent_db)) == 1


def test_start_run_refuses_over_the_hourly_limit(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[int] = []
    monkeypatch.setattr(
        runner, "submit", lambda run_id, *, engine, settings: submitted.append(run_id)
    )
    settings = agent_db.settings.model_copy(update={"agent_visitor_runs_per_hour": 1})
    kw: dict[str, Any] = {
        "visitor_id": "v",
        "profile_id": 1,
        "regchange_id": agent_db.change_id,
        "now": utcnow(),
    }
    first, _ = start_run(agent_db.engine, settings, **kw)
    assert first is not None
    _set(agent_db, first, status="done")
    second, refusal = start_run(agent_db.engine, settings, **kw)
    assert second is None and "in the last hour" in refusal and submitted == [first]


def test_concurrent_starts_make_one_run(agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruling 1: two start requests at once (sync routes run in a threadpool) → one run; the
    hourly count cannot be passed twice either."""
    submitted: list[int] = []
    monkeypatch.setattr(
        runner, "submit", lambda run_id, *, engine, settings: submitted.append(run_id)
    )
    settings = agent_db.settings.model_copy(update={"agent_visitor_runs_per_hour": 1})
    kw: dict[str, Any] = {
        "visitor_id": "v",
        "profile_id": 1,
        "regchange_id": agent_db.change_id,
        "now": utcnow(),
    }
    n = 4
    barrier = threading.Barrier(n)
    results: list[tuple[int | None, str]] = []
    errors: list[BaseException] = []

    def press() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(start_run(agent_db.engine, settings, **kw))
        except BaseException as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=press) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    (run,) = _all_runs(agent_db)
    assert results == [(run.id, "")] * n and submitted == [run.id]


def test_start_run_expires_a_stale_waiting_run_first(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 2: a waiting run past WAIT_LIMIT is expired (persisted) before start_check counts
    it as active, so the visitor gets a new run instead of the dead one."""
    submitted: list[int] = []
    monkeypatch.setattr(
        runner, "submit", lambda run_id, *, engine, settings: submitted.append(run_id)
    )
    now = utcnow()
    stale = new_run(agent_db, visitor="v")
    _set(agent_db, stale, status="waiting", updated_at=now - timedelta(minutes=31))
    kw: dict[str, Any] = {
        "visitor_id": "v",
        "profile_id": 1,
        "regchange_id": agent_db.change_id,
        "now": now,
    }
    fresh, refusal = start_run(agent_db.engine, agent_db.settings, **kw)
    assert fresh is not None and fresh != stale and refusal == "" and submitted == [fresh]
    assert (_run(agent_db, stale).status, _run(agent_db, stale).stop_reason) == (
        "expired",
        EXPIRED,
    )


def test_start_run_rejoins_a_fresh_waiting_run(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[int] = []
    monkeypatch.setattr(
        runner, "submit", lambda run_id, *, engine, settings: submitted.append(run_id)
    )
    now = utcnow()
    waiting = new_run(agent_db, visitor="v")
    _set(agent_db, waiting, status="waiting", updated_at=now - timedelta(minutes=29))
    kw: dict[str, Any] = {
        "visitor_id": "v",
        "profile_id": 1,
        "regchange_id": agent_db.change_id,
        "now": now,
    }
    assert start_run(agent_db.engine, agent_db.settings, **kw) == (waiting, "")
    assert _run(agent_db, waiting).status == "waiting" and submitted == []


def test_answer_submits_only_a_valid_index(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = new_run(agent_db)
    advance(
        run_id,
        engine=agent_db.engine,
        settings=agent_db.settings,
        llm=ScriptedLLM(until_question()),
        tavily=ihk_tavily(),
        budget=Budget(),
    )
    submitted: list[int] = []
    monkeypatch.setattr(
        runner, "submit", lambda run_id, *, engine, settings: submitted.append(run_id)
    )
    assert runner.answer(agent_db.engine, agent_db.settings, run_id, 5) == "bad_option"
    assert submitted == []
    assert runner.answer(agent_db.engine, agent_db.settings, run_id, 0) == "ok"
    assert submitted == [run_id]
    assert runner.answer(agent_db.engine, agent_db.settings, run_id, 0) == "not_waiting"
    assert submitted == [run_id]


def test_answer_expires_a_stale_wait_before_recording(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = new_run(agent_db)
    advance(
        run_id,
        engine=agent_db.engine,
        settings=agent_db.settings,
        llm=ScriptedLLM(until_question()),
        tavily=ihk_tavily(),
        budget=Budget(),
    )
    _set(agent_db, run_id, updated_at=utcnow() - timedelta(minutes=31))
    submitted: list[int] = []
    monkeypatch.setattr(
        runner, "submit", lambda run_id, *, engine, settings: submitted.append(run_id)
    )
    assert runner.answer(agent_db.engine, agent_db.settings, run_id, 0) == "not_waiting"
    assert (_run(agent_db, run_id).status, submitted) == ("expired", [])


@pytest.mark.parametrize("claimed", [False, True])
def test_run_now_fails_the_run_when_advance_raises(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch, claimed: bool
) -> None:
    monkeypatch.setattr(
        runner,
        "make_backends",
        lambda settings: Backends(llm=ScriptedLLM([]), tavily=ScriptedTavily()),
    )
    run_id = new_run(agent_db)

    def boom(rid: int, **kw: Any) -> str:
        if claimed:
            _set(agent_db, rid, status="running")
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runner, "advance", boom)
    runner.run_now(run_id, engine=agent_db.engine, settings=agent_db.settings)  # never raises
    run = _run(agent_db, run_id)
    assert (run.status, run.stop_reason) == ("failed", "internal error")
