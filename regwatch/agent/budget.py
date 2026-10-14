"""Spend limits for the agent, checked before every call (spec §2.3).

Day = UTC date. "Spent" is the sum over AgentStepRow created that day, so a run across midnight
splits correctly, plus whatever in-flight calls have reserved. A model call reserves its worst case
(estimated prompt tokens × in-price + 4,096 × out-price). A Tavily call reserves 1 credit against
both the run and the day. The step row then settles the real numbers and the reservation is
released. Reservations live in memory behind one lock, which is correct only for ONE process:
`regwatch serve` must stay single-worker. A second worker would need `BEGIN IMMEDIATE` instead.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from regwatch.config import Settings
from regwatch.db import AGENT_ACTIVE, AgentRunRow, AgentStepRow, session

MAX_COMPLETION_TOKENS = 4_096  # LiveLLMBackend's max_tokens
CHARS_PER_TOKEN = 3.5
DAILY_BUDGET_USED = "daily budget used"
RUN_BUDGET_USED = "run budget used"
HOURLY_LIMIT = "You've started {n} checks in the last hour. Try again later."
NO_BUDGET = "The agent has used today's budget. Try again tomorrow (UTC)."


@dataclass(frozen=True)
class Reservation:
    key: int
    run_id: int
    usd: float
    credits: int


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today(s: Session, now: datetime) -> tuple[float, int]:
    """(USD, Tavily credits) settled by agent steps since 00:00 UTC of `now` (naive UTC)."""
    since = col(AgentStepRow.created_at) >= day_start(now)
    usd = s.exec(
        select(func.coalesce(func.sum(col(AgentStepRow.cost_usd)), 0.0)).where(since)
    ).one()
    credits = s.exec(
        select(func.coalesce(func.sum(col(AgentStepRow.credits)), 0)).where(since)
    ).one()
    return float(usd), int(credits)


def worst_case_usd(settings: Settings, prompt_chars: int) -> float:
    return (
        prompt_chars / CHARS_PER_TOKEN * settings.classifier_price_in_per_mtok
        + MAX_COMPLETION_TOKENS * settings.classifier_price_out_per_mtok
    ) / 1_000_000


class Budget:
    """This process's in-flight reservations; `BUDGET` is the one instance the app uses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[int, Reservation] = {}
        self._next = 0

    def _held_for(self, run_id: int | None) -> tuple[float, int]:
        held = [r for r in self._held.values() if run_id is None or r.run_id == run_id]
        return sum(r.usd for r in held), sum(r.credits for r in held)

    def _hold(self, run_id: int, usd: float, credits: int) -> Reservation:
        self._next += 1
        res = Reservation(key=self._next, run_id=run_id, usd=usd, credits=credits)
        self._held[res.key] = res
        return res

    def reserve_model(
        self, engine: Engine, settings: Settings, run_id: int, *, prompt_chars: int, now: datetime
    ) -> Reservation | str:
        need = worst_case_usd(settings, prompt_chars)
        with self._lock, session(engine) as s:
            run = s.get(AgentRunRow, run_id)
            if run is None or run.n_steps >= settings.agent_run_steps:
                return RUN_BUDGET_USED
            usd, _ = spent_today(s, now)
            held_usd, _ = self._held_for(None)
            if usd + held_usd + need > settings.agent_daily_usd:
                return DAILY_BUDGET_USED
            return self._hold(run_id, need, 0)

    def reserve_tavily(
        self, engine: Engine, settings: Settings, run_id: int, *, now: datetime
    ) -> Reservation | str:
        with self._lock, session(engine) as s:
            run = s.get(AgentRunRow, run_id)
            _, run_held = self._held_for(run_id)
            if run is None or run.tavily_credits + run_held + 1 > settings.agent_run_credits:
                return RUN_BUDGET_USED
            _, credits = spent_today(s, now)
            _, held = self._held_for(None)
            if credits + held + 1 > settings.agent_daily_credits:
                return DAILY_BUDGET_USED
            return self._hold(run_id, 0.0, 1)

    def release(self, res: Reservation) -> None:
        with self._lock:
            self._held.pop(res.key, None)

    def day_left(self, engine: Engine, settings: Settings, now: datetime) -> tuple[float, int]:
        """(USD, credits) still available today for everyone together."""
        with self._lock, session(engine) as s:
            usd, credits = spent_today(s, now)
            held_usd, held_credits = self._held_for(None)
        return (
            settings.agent_daily_usd - usd - held_usd,
            settings.agent_daily_credits - credits - held_credits,
        )


BUDGET = Budget()


@dataclass(frozen=True)
class StartCheck:
    active_id: int | None  # the visitor's queued/running/waiting run for this change
    refusal: str  # "" = a new run may start


def start_check(
    engine: Engine,
    settings: Settings,
    *,
    visitor_id: str,
    regchange_id: int,
    now: datetime,
    budget: Budget = BUDGET,
) -> StartCheck:
    """One active run per visitor+change (rejoined, not duplicated); then the visitor's hourly
    limit; then the day's budget must still cover one worst-case call and one credit."""
    with session(engine) as s:
        active = s.exec(
            select(AgentRunRow)
            .where(
                AgentRunRow.visitor_id == visitor_id,
                AgentRunRow.regchange_id == regchange_id,
                col(AgentRunRow.status).in_(AGENT_ACTIVE),
            )
            .order_by(col(AgentRunRow.id).desc())
        ).first()
        if active is not None:
            return StartCheck(active_id=active.id, refusal="")
        n = int(
            s.exec(
                select(func.count(col(AgentRunRow.id))).where(
                    AgentRunRow.visitor_id == visitor_id,
                    col(AgentRunRow.created_at) >= now - timedelta(hours=1),
                )
            ).one()
        )
    if n >= settings.agent_visitor_runs_per_hour:
        return StartCheck(active_id=None, refusal=HOURLY_LIMIT.format(n=n))
    usd, credits = budget.day_left(engine, settings, now)
    if usd < worst_case_usd(settings, 0) or credits < 1:
        return StartCheck(active_id=None, refusal=NO_BUDGET)
    return StartCheck(active_id=None, refusal="")
