from datetime import datetime, timedelta
from typing import Any

import pytest

from regwatch.agent.budget import (
    DAILY_BUDGET_USED,
    NO_BUDGET,
    RUN_BUDGET_USED,
    Budget,
    Reservation,
    StartCheck,
    spent_today,
    start_check,
    worst_case_usd,
)
from regwatch.config import Settings
from regwatch.db import make_engine, session
from tests.conftest import make_agent_run as _run
from tests.conftest import make_agent_step

NOW = datetime(2026, 10, 20, 12, 0)


@pytest.fixture()
def engine(tmp_settings: Settings) -> Any:
    return make_engine(tmp_settings)


def _spend(
    engine: Any, run_id: int, *, usd: float = 0.0, credits: int = 0, at: datetime = NOW
) -> None:
    make_agent_step(engine, run_id, usd=usd, credits=credits, at=at)


def test_worst_case_is_prompt_estimate_plus_full_completion(tmp_settings: Settings) -> None:
    expected = (1_000 * 1.00 + 4_096 * 3.00) / 1_000_000
    assert worst_case_usd(tmp_settings, 3_500) == pytest.approx(expected)


def test_run_step_limit(engine: Any, tmp_settings: Settings) -> None:
    full = _run(engine, n_steps=6)
    got = Budget().reserve_model(engine, tmp_settings, full, prompt_chars=0, now=NOW)
    assert got == RUN_BUDGET_USED
    ok = Budget().reserve_model(
        engine, tmp_settings, _run(engine, n_steps=5), prompt_chars=0, now=NOW
    )
    assert isinstance(ok, Reservation)


def test_run_credit_limit_counts_in_flight(engine: Any, tmp_settings: Settings) -> None:
    budget, run = Budget(), _run(engine, credits=9)
    assert isinstance(budget.reserve_tavily(engine, tmp_settings, run, now=NOW), Reservation)
    assert budget.reserve_tavily(engine, tmp_settings, run, now=NOW) == RUN_BUDGET_USED
    spent = _run(engine, credits=10)
    assert Budget().reserve_tavily(engine, tmp_settings, spent, now=NOW) == RUN_BUDGET_USED


def test_daily_usd_stop_counts_only_today(engine: Any, tmp_settings: Settings) -> None:
    run = _run(engine)
    _spend(engine, run, usd=0.995, at=NOW - timedelta(days=1))
    first = Budget().reserve_model(engine, tmp_settings, run, prompt_chars=0, now=NOW)
    assert isinstance(first, Reservation)
    _spend(engine, run, usd=0.995, at=NOW.replace(hour=0, minute=1))
    second = Budget().reserve_model(engine, tmp_settings, run, prompt_chars=0, now=NOW)
    assert second == DAILY_BUDGET_USED


def test_daily_credit_stop_and_in_flight_across_runs(engine: Any, tmp_settings: Settings) -> None:
    a, b = _run(engine), _run(engine, visitor="w")
    _spend(engine, a, credits=59)
    budget = Budget()
    held = budget.reserve_tavily(engine, tmp_settings, a, now=NOW)
    assert isinstance(held, Reservation)
    assert budget.reserve_tavily(engine, tmp_settings, b, now=NOW) == DAILY_BUDGET_USED
    budget.release(held)
    assert isinstance(budget.reserve_tavily(engine, tmp_settings, b, now=NOW), Reservation)


def test_reservation_refuses_a_call_that_would_overshoot(
    engine: Any, tmp_settings: Settings
) -> None:
    settings = tmp_settings.model_copy(update={"agent_daily_usd": 0.0125})
    run = _run(engine)
    budget = Budget()
    held = budget.reserve_model(engine, settings, run, prompt_chars=0, now=NOW)  # $0.012288
    assert isinstance(held, Reservation)
    again = budget.reserve_model(engine, settings, run, prompt_chars=0, now=NOW)
    assert again == DAILY_BUDGET_USED
    budget.release(held)
    big = budget.reserve_model(engine, settings, run, prompt_chars=3_500, now=NOW)  # $0.013288
    assert big == DAILY_BUDGET_USED


def test_spent_today_splits_at_utc_midnight(engine: Any) -> None:
    run = _run(engine)
    _spend(engine, run, usd=0.10, credits=3, at=NOW.replace(hour=0) - timedelta(minutes=1))
    _spend(engine, run, usd=0.02, credits=1, at=NOW.replace(hour=0, minute=1))
    with session(engine) as s:
        usd, credits = spent_today(s, NOW)
    assert usd == pytest.approx(0.02) and credits == 1


def test_one_active_run_per_visitor_and_change(engine: Any, tmp_settings: Settings) -> None:
    def check(visitor: str, change: int) -> StartCheck:
        return start_check(
            engine, tmp_settings, visitor_id=visitor, regchange_id=change, now=NOW, budget=Budget()
        )

    active = _run(engine, status="waiting")
    assert check("v", 14).active_id == active
    assert check("w", 14).active_id is None
    assert check("v", 3).active_id is None
    _run(engine, visitor="x", status="done")
    assert check("x", 14) == StartCheck(active_id=None, refusal="")


def test_visitor_hourly_limit(engine: Any, tmp_settings: Settings) -> None:
    for i in range(5):
        _run(engine, change=100 + i, status="done", created_at=NOW - timedelta(minutes=50))
    _run(engine, change=200, status="done", created_at=NOW - timedelta(minutes=61))
    check = start_check(
        engine, tmp_settings, visitor_id="v", regchange_id=14, now=NOW, budget=Budget()
    )
    assert check.active_id is None and "5 checks in the last hour" in check.refusal


def test_start_refused_without_daily_budget(engine: Any, tmp_settings: Settings) -> None:
    _spend(engine, _run(engine, visitor="other", status="done"), credits=60)
    check = start_check(
        engine, tmp_settings, visitor_id="v", regchange_id=14, now=NOW, budget=Budget()
    )
    assert check.refusal == NO_BUDGET
