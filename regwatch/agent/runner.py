"""Where agent runs execute: a module-level ThreadPoolExecutor(max_workers=2) in the ONE uvicorn
process (`regwatch serve`). More workers are unsupported, because each would get its own executor,
budget lock and start lock. `submit` queues `advance`; `sweep_interrupted` runs at app startup; a
waiting run expires 30 minutes after its question, checked whenever it is read (and before a start
counts it as the visitor's active run).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import col, update

from regwatch.agent.budget import start_check
from regwatch.agent.loop import AnswerResult, advance, record_answer, transition
from regwatch.config import Settings
from regwatch.db import AgentRunRow, session, utcnow
from regwatch.ingest.tavily_backend import TavilyBackend, make_backend
from regwatch.understand.llm_backend import LiveLLMBackend, LLMBackend

log = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="regwatch-agent")
# Held across expiry + start_check + insert: start_check only reads, so without this lock two
# concurrent starts (sync routes run in a threadpool) could both pass and insert two runs.
_START_LOCK = threading.Lock()
WAIT_LIMIT = timedelta(minutes=30)
INTERRUPTED = "interrupted by restart"
EXPIRED = "no answer within 30 minutes"
UNAVAILABLE = "the agent's model or search service is not configured"


@dataclass(frozen=True)
class Backends:
    llm: LLMBackend
    tavily: TavilyBackend


def live_backends(settings: Settings) -> Backends:
    """Token Factory + Tavily; both refuse an empty key before any network call."""
    return Backends(llm=LiveLLMBackend(settings), tavily=make_backend(settings, "live"))


make_backends: Callable[[Settings], Backends] = live_backends


def run_now(run_id: int, *, engine: Engine, settings: Settings) -> None:
    """Executor body: build the backends, then advance. Never raises."""
    try:
        backends = make_backends(settings)
    except Exception as e:
        log.error("agent run=%d: backends unavailable (%s)", run_id, type(e).__name__)
        transition(engine, run_id, from_=("queued",), to="failed", stop_reason=UNAVAILABLE)
        return
    advance(run_id, engine=engine, settings=settings, llm=backends.llm, tavily=backends.tavily)


def submit(run_id: int, *, engine: Engine, settings: Settings) -> Future[None]:
    return _EXECUTOR.submit(run_now, run_id, engine=engine, settings=settings)


def _expire(engine: Engine, *where: Any, now: datetime) -> int:
    """waiting → expired for runs matching `where` whose question is older than WAIT_LIMIT."""
    with session(engine) as s:
        res = s.exec(
            update(AgentRunRow)
            .where(
                col(AgentRunRow.status) == "waiting",
                col(AgentRunRow.updated_at) < now - WAIT_LIMIT,
                *where,
            )
            .values(status="expired", stop_reason=EXPIRED, updated_at=now)
        )
        s.commit()
        return int(res.rowcount)


def start_run(
    engine: Engine,
    settings: Settings,
    *,
    visitor_id: str,
    profile_id: int,
    regchange_id: int,
    now: datetime,
) -> tuple[int | None, str]:
    """(run id, refusal). Expires the visitor's stale waiting run for this change, then rejoins
    their active run; otherwise checks the hourly limit and the day's budget, inserts a queued run
    and submits it. One process-wide lock covers the check and the insert."""
    with _START_LOCK:
        _expire(
            engine,
            col(AgentRunRow.visitor_id) == visitor_id,
            col(AgentRunRow.regchange_id) == regchange_id,
            now=now,
        )
        check = start_check(
            engine, settings, visitor_id=visitor_id, regchange_id=regchange_id, now=now
        )
        if check.active_id is not None:
            return check.active_id, ""
        if check.refusal:
            return None, check.refusal
        with session(engine) as s:
            row = AgentRunRow(
                visitor_id=visitor_id,
                profile_id=profile_id,
                regchange_id=regchange_id,
                created_at=now,
                updated_at=now,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            assert row.id is not None
            run_id = row.id
    submit(run_id, engine=engine, settings=settings)
    return run_id, ""


def answer(engine: Engine, settings: Settings, run_id: int, option: int) -> AnswerResult:
    """Store the visitor's option (waiting → queued) and submit; nothing else is accepted. A
    question past its 30 minutes is expired first, so a late answer is refused as not_waiting."""
    expire_if_stale(engine, run_id, now=utcnow())
    result = record_answer(engine, run_id, option)
    if result == "ok":
        submit(run_id, engine=engine, settings=settings)
    return result


def sweep_interrupted(engine: Engine) -> int:
    """At startup: runs a previous process left queued/running can never finish → failed."""
    with session(engine) as s:
        res = s.exec(
            update(AgentRunRow)
            .where(col(AgentRunRow.status).in_(("running", "queued")))
            .values(status="failed", stop_reason=INTERRUPTED, updated_at=utcnow())
        )
        s.commit()
        n = int(res.rowcount)
    if n:
        log.warning("agent: %d run(s) %s", n, INTERRUPTED)
    return n


def expire_if_stale(engine: Engine, run_id: int, *, now: datetime) -> bool:
    """waiting → expired when its question is older than WAIT_LIMIT; checked when read."""
    return _expire(engine, col(AgentRunRow.id) == run_id, now=now) == 1
