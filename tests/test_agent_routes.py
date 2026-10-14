"""Agent routes on the change page (spec §3). Runs execute synchronously in-process with
ScriptedLLM / ScriptedTavily, through runner.run_now."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlmodel import col, select

from regwatch.agent import runner
from regwatch.agent.budget import NO_BUDGET
from regwatch.agent.runner import Backends
from regwatch.db import AgentFactRow, AgentRunRow, ProfileRow, session, utcnow
from tests.conftest import ART_11A, EUDR_UNCLEAR, seed_change_id
from tests.fakes import (
    IHK_URL,
    OPTIONS,
    QUESTION,
    TRADER_QUOTE,
    ScriptedLLM,
    ScriptedTavily,
    agent_action,
    decide_does_not,
    decide_unclear,
    ihk_tavily,
    until_question,
)

EUDR_ID = seed_change_id(EUDR_UNCLEAR)
START = f"/change/{EUDR_ID}/agent"
HX = {"HX-Request": "true"}
DOESNT = str(escape("doesn't apply"))  # Jinja escapes the apostrophe: doesn&#39;t apply


@dataclass
class Harness:
    client: TestClient
    app: Any
    llm: ScriptedLLM
    tavily: ScriptedTavily

    def runs(self) -> list[AgentRunRow]:
        with session(self.app.engine) as s:
            return list(s.exec(select(AgentRunRow).order_by(col(AgentRunRow.id))).all())


@pytest.fixture()
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Harness]:
    def _make(
        *,
        enabled: bool = True,
        locked: bool = False,
        replies: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> Harness:
        monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'agent.db'}")
        monkeypatch.setenv("REGWATCH_AGENT_ENABLED", "true" if enabled else "false")
        monkeypatch.setenv("REGWATCH_PROFILE_LOCKED", "true" if locked else "false")
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        import regwatch.app as appmod

        appmod = importlib.reload(appmod)
        from regwatch.jobs.crawl import seed

        seed()
        llm, tavily = ScriptedLLM(list(replies or [])), ihk_tavily()
        monkeypatch.setattr(
            runner, "make_backends", lambda settings: Backends(llm=llm, tavily=tavily)
        )
        monkeypatch.setattr(
            runner,
            "submit",
            lambda run_id, *, engine, settings: runner.run_now(
                run_id, engine=engine, settings=settings
            ),
        )
        return Harness(TestClient(appmod.app), appmod, llm, tavily)

    return _make


def _footer(html: str) -> str:
    return html.split("<footer>", 1)[1]


def test_button_only_on_unclear_when_enabled(make: Callable[..., Harness]) -> None:
    h = make()
    html = h.client.get(f"/change/{EUDR_ID}").text
    assert "it searches the official sources and may ask you one or two questions" in html
    assert f'hx-post="{START}"' in html and "Or answer it in your store profile" in html
    assert html.count("htmx.org@2.0.4") == 1
    assert "Let the agent check" not in h.client.get(f"/change/{seed_change_id(ART_11A)}").text


def test_kill_switch_hides_button_and_refuses_with_404(make: Callable[..., Harness]) -> None:
    h = make(enabled=False)
    html = h.client.get(f"/change/{EUDR_ID}").text
    assert "Let the agent check" not in html and "Answer one question in your store profile" in html
    assert h.client.post(START, headers=HX).status_code == 404
    assert h.client.get("/agent/1", headers=HX).status_code == 404
    assert h.client.post("/agent/1/answer", data={"option": "0"}, headers=HX).status_code == 404
    assert h.runs() == []


def test_start_needs_htmx_or_same_origin(make: Callable[..., Harness]) -> None:
    h = make(replies=until_question())
    assert h.client.post(START).status_code == 403
    assert h.client.post(START, headers={"Origin": "https://evil.example"}).status_code == 403
    assert h.client.post(START, headers={"Referer": "https://evil.example/x"}).status_code == 403
    assert h.runs() == []
    r = h.client.post(START, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/change/{EUDR_ID}"
    assert len(h.runs()) == 1


def test_start_only_on_an_unclear_change(make: Callable[..., Harness]) -> None:
    h = make()
    assert h.client.post(f"/change/{seed_change_id(ART_11A)}/agent", headers=HX).status_code == 404
    assert h.client.post("/change/9999/agent", headers=HX).status_code == 404


def test_full_flow_trace_answer_overlay(make: Callable[..., Harness]) -> None:
    h = make(replies=[*until_question(), decide_does_not()])
    footer_before = _footer(h.client.get("/feed").text)
    r = h.client.post(START, headers=HX)
    assert r.status_code == 200 and r.text.lstrip().startswith('<div id="agent-run"')
    cookie = r.headers["set-cookie"].lower()
    assert "rw_visitor=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert len(h.client.cookies.get("rw_visitor") or "") == 32
    assert (
        "Searched <strong>ihk.de</strong> for “EUDR Händler EU-Lieferanten Marktteilnehmer” "
        "— 1 result · 1 credit"
    ) in r.text
    assert "Read <strong>www.ihk.de/…</strong> · 1 credit" in r.text
    assert str(escape(QUESTION)) in r.text
    assert '<button type="submit" name="option" value="1">' in r.text
    assert "hx-trigger" not in r.text
    run_id = h.runs()[0].id
    r = h.client.post(f"/agent/{run_id}/answer", data={"option": "1"}, headers=HX)
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    assert f"Decided: <strong>{DOESNT}</strong>" in r.text
    assert "4 steps · 2 credits · $0.010" in r.text and "hx-trigger" not in r.text
    page = h.client.get(f"/change/{EUDR_ID}").text
    assert f"Agent: {DOESNT}" in page
    assert str(escape(f"You told us: {OPTIONS[1]}")) in page
    assert str(escape(TRADER_QUOTE)) in page and f'href="{IHK_URL}"' in page
    assert "Only you see this; the shared demo is unchanged." in page
    assert page.index("Agent: ") < page.index("Original check") < page.index("We couldn")
    assert _footer(h.client.get("/feed").text) == footer_before


def test_answer_accepts_an_option_index_only(make: Callable[..., Harness]) -> None:
    h = make(replies=until_question())
    h.client.post(START, headers=HX)
    run_id = h.runs()[0].id
    for bad in (OPTIONS[1], "5", "-1", "1.0", ""):
        r = h.client.post(f"/agent/{run_id}/answer", data={"option": bad}, headers=HX)
        assert r.status_code in (400, 422), bad
    assert h.client.post(f"/agent/{run_id}/answer", data={"option": "1"}).status_code == 403
    with session(h.app.engine) as s:
        (fact,) = s.exec(select(AgentFactRow)).all()
    assert fact.chosen_index is None and h.runs()[0].status == "waiting"


def test_another_visitors_run_is_404(make: Callable[..., Harness]) -> None:
    h = make(replies=until_question())
    h.client.post(START, headers=HX)
    run_id = h.runs()[0].id
    other = TestClient(h.app.app)
    assert other.get(f"/agent/{run_id}", headers=HX).status_code == 404
    assert (
        other.post(f"/agent/{run_id}/answer", data={"option": "0"}, headers=HX).status_code == 404
    )
    page = other.get(f"/change/{EUDR_ID}").text
    assert str(escape(QUESTION)) not in page and f'hx-post="{START}"' in page


def test_polling_only_while_queued_or_running(make: Callable[..., Harness]) -> None:
    h = make()
    with session(h.app.engine) as s:
        row = AgentRunRow(visitor_id="v-test", profile_id=1, regchange_id=EUDR_ID)
        s.add(row)
        s.commit()
        s.refresh(row)
        run_id = row.id
    h.client.cookies.set("rw_visitor", "v-test")
    r = h.client.get(f"/agent/{run_id}", headers=HX)
    assert f'hx-get="/agent/{run_id}" hx-trigger="every 1.5s"' in r.text
    assert r.headers.get("HX-Refresh") is None
    with session(h.app.engine) as s:
        row = s.get(AgentRunRow, run_id)
        assert row is not None
        row.status = "done"
        s.add(row)
        s.commit()
    r = h.client.get(f"/agent/{run_id}", headers=HX)
    assert "hx-trigger" not in r.text and r.headers.get("HX-Refresh") == "true"


def test_start_twice_reuses_the_active_run(make: Callable[..., Harness]) -> None:
    h = make(replies=until_question())
    h.client.post(START, headers=HX)
    h.client.post(START, headers=HX)
    assert len(h.runs()) == 1 and len(h.llm.calls) == 3


def test_hourly_limit_refuses_with_a_message(make: Callable[..., Harness]) -> None:
    h = make(replies=[decide_unclear()], env={"REGWATCH_AGENT_VISITOR_RUNS_PER_HOUR": "1"})
    h.client.post(START, headers=HX)
    r = h.client.post(START, headers=HX)
    assert r.status_code == 200 and len(h.runs()) == 1
    assert str(escape("You've started 1 check in the last hour")) in r.text
    assert "1 checks" not in r.text


def test_budget_refusal_is_said_once(make: Callable[..., Harness]) -> None:
    h = make(env={"REGWATCH_AGENT_DAILY_CREDITS": "0"})
    page = h.client.get(f"/change/{EUDR_ID}").text
    assert page.count(str(escape(NO_BUDGET))) == 1 and f'hx-post="{START}"' not in page
    r = h.client.post(START, headers=HX)
    assert r.status_code == 200 and r.text.count(str(escape(NO_BUDGET))) == 1
    assert h.runs() == []


def test_answer_after_expiry_is_refused(make: Callable[..., Harness]) -> None:
    h = make(replies=until_question())
    h.client.post(START, headers=HX)
    run_id = h.runs()[0].id
    with session(h.app.engine) as s:
        row = s.get(AgentRunRow, run_id)
        assert row is not None
        row.updated_at = utcnow() - timedelta(minutes=31)
        s.add(row)
        s.commit()
    assert (
        h.client.post(f"/agent/{run_id}/answer", data={"option": "1"}, headers=HX).status_code
        == 409
    )
    assert h.runs()[0].status == "expired"
    page = h.client.get(f"/change/{EUDR_ID}").text
    assert "Stopped: no answer within 30 minutes" in page and "Try again" in page


def test_runs_of_an_old_profile_are_ignored(make: Callable[..., Harness]) -> None:
    h = make(replies=[*until_question(), decide_does_not()])
    h.client.post(START, headers=HX)
    run_id = h.runs()[0].id
    h.client.post(f"/agent/{run_id}/answer", data={"option": "1"}, headers=HX)
    with session(h.app.engine) as s:
        s.add(ProfileRow(establishment_country="DE", ship_to='["DE"]', categories='["leather"]'))
        s.commit()
    page = h.client.get(f"/change/{EUDR_ID}").text
    assert "Not checked against your profile yet" in page and "Agent: " not in page
    assert h.client.get(f"/agent/{run_id}", headers=HX).status_code == 404


def test_answer_on_a_run_of_an_old_profile_is_404(make: Callable[..., Harness]) -> None:
    h = make(replies=until_question())
    h.client.post(START, headers=HX)
    run_id = h.runs()[0].id
    with session(h.app.engine) as s:
        s.add(ProfileRow(establishment_country="DE", ship_to='["DE"]', categories='["leather"]'))
        s.commit()
    r = h.client.post(f"/agent/{run_id}/answer", data={"option": "1"}, headers=HX)
    assert r.status_code == 404
    with session(h.app.engine) as s:
        (fact,) = s.exec(select(AgentFactRow)).all()
    assert fact.chosen_index is None and h.runs()[0].status == "waiting"


def test_untrusted_strings_are_escaped(make: Callable[..., Harness]) -> None:
    evil = "<script>alert(1)</script>"
    h = make(
        replies=[
            agent_action(
                "ask_merchant",
                thought=evil,
                question=f"Do you sell {evil}?",
                options=["<b>yes</b>", "No"],
            )
        ]
    )
    r = h.client.post(START, headers=HX)
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "<b>yes</b>" not in r.text


def test_profile_lock(make: Callable[..., Harness]) -> None:
    h = make(locked=True)
    page = h.client.get("/profile").text
    assert "can't be changed here" in page and 'class="plain" disabled' in page
    assert 'type="submit"' not in page
    r = h.client.post(
        "/profile",
        data={"establishment_country": "FR", "ship_to": ["FR"], "categories": ["leather"]},
        follow_redirects=False,
    )
    assert r.status_code == 403
    with session(h.app.engine) as s:
        assert len(s.exec(select(ProfileRow)).all()) == 1
