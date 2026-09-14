"""Month view (Oct 22): buckets by due date, overdue first, htmx check-off with a plain-form
fallback, empty and unclear states. Templates display stored data only."""

from __future__ import annotations

import importlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from regwatch.config import REGULATION_LABELS
from regwatch.db import ApplicabilityRow, DocumentRow, RegChangeRow, TodoStateRow, session

TODAY = date(2026, 9, 14)
WITHDRAWAL = REGULATION_LABELS["withdrawal"]
EUDR = REGULATION_LABELS["eudr"]
OVERDUE_19 = 'in force since <time datetime="2026-06-19">19 June 2026</time>'
OVERDUE_30 = 'in force since <time datetime="2026-06-30">30 June 2026</time>'


def _reload_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Any:
    """Fresh DB + reloaded app module with `_today` pinned to TODAY (never a default arg)."""
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / name}")
    import regwatch.app as appmod

    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_today", lambda: TODAY)
    return appmod


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The recorded seed run: two applies changes, four to-dos, all in force before TODAY."""
    appmod = _reload_frozen(tmp_path, monkeypatch, "month.db")
    from regwatch.jobs.crawl import seed

    assert seed() == 2
    return TestClient(appmod.app)


@pytest.fixture()
def empty_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """No rows at all, then one saved profile (so /month renders instead of redirecting)."""
    appmod = _reload_frozen(tmp_path, monkeypatch, "empty.db")
    c = TestClient(appmod.app)
    r = c.post(
        "/profile",
        data={"establishment_country": "DE", "ship_to": ["DE"], "categories": ["leather"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return c


def _profile_id() -> int:
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        prof = appmod.current_profile(s)
    assert prof is not None and prof.id is not None
    return prof.id


def _add_change(
    *,
    url: str,
    instrument: str,
    effective_dates: list[date],
    decision: str,
    reasons: list[str],
    todos: list[tuple[str, date | None]],
    tag: str = "withdrawal",
) -> int:
    """One document + change (one obligation) + applicability whose to-dos cite that obligation's
    quote and the document URL — the same grounding shape the crawl stores. Returns change id."""
    import regwatch.app as appmod

    quote = f"Verbatim sentence of {instrument} that binds the trader."
    domain = urlparse(url).hostname or ""
    with session(appmod.engine) as s:
        s.add(
            DocumentRow(
                url=url,
                content_hash=f"hash-{url}",
                source_domain=domain,
                published_date="2026-08-01",
                fetched_at=datetime(2026, 8, 2, 8, 0),
                markdown=f"# {instrument}\n\n{quote}\n",
                regulation_tags=json.dumps([tag]),
            )
        )
        ch = RegChangeRow(
            document_url=url,
            content_hash=f"hash-{url}",
            model="nvidia/Nemotron-3_5-Lightning",
            instrument=instrument,
            jurisdiction="DE",
            summary=f"Summary of {instrument}.",
            effective_dates=json.dumps([d.isoformat() for d in effective_dates]),
            obligations=json.dumps([{"text": f"Obey {instrument}.", "quote": quote}]),
            created_at=datetime(2026, 8, 3, 9, 0),
        )
        s.add(ch)
        s.commit()
        s.refresh(ch)
        assert ch.id is not None
        s.add(
            ApplicabilityRow(
                profile_id=_profile_id(),
                regchange_id=ch.id,
                decision=decision,
                reasons=json.dumps(reasons),
                todos=json.dumps(
                    [
                        {
                            "text": text,
                            "due": due.isoformat() if due else None,
                            "quote": quote,
                            "citation_url": url,
                        }
                        for text, due in todos
                    ]
                ),
                model="nvidia/Nemotron-3-Ultra-550b-a55b",
                cost_usd=0.01,
            )
        )
        s.commit()
        return ch.id


def _keys(html: str) -> list[str]:
    return [chunk.split("/")[0] for chunk in html.split('action="/todo/')[1:]]


def _subline(html: str) -> str:
    return html.split('<p class="muted">')[1].split("</p>")[0]


# ---------- 1–2: buckets on the seed run and on added rows ----------


def test_seed_both_groups_in_force(client: TestClient) -> None:
    html = client.get("/month").text
    assert html.count("<h2>Already in force</h2>") == 1
    assert "The rule already applies — do these now." in html
    assert html.count(OVERDUE_19) == 2
    assert html.count(OVERDUE_30) == 2
    assert html.count('class="pill overdue"') == 4
    assert "Coming up" not in html
    assert "Due this month" not in html
    assert "Nothing due" not in html
    assert _subline(html) == "September 2026 · 4 open · 0 done · 0 unclear"
    # R5: every row links its change and its source
    assert html.count('href="/change/1">' + WITHDRAWAL) == 2
    assert html.count('href="/change/2">' + EUDR) == 2
    assert html.count('rel="noopener">source</a>') == 4


def test_buckets_and_order(client: TestClient) -> None:
    _add_change(
        url="https://www.gesetze-im-internet.de/bgb/__356a.html",
        instrument="§ 356a BGB",
        effective_dates=[date(2027, 1, 1)],
        decision="applies",
        reasons=["You are established in Germany."],
        todos=[("Relabel the button in German", date(2027, 1, 1))],
    )
    _add_change(
        url="https://www.bmj.de/widerrufsbutton-faq",
        instrument="BMJ FAQ",
        effective_dates=[],
        decision="applies",
        reasons=["You sell to German consumers."],
        todos=[("Check the FAQ wording against your checkout", date(2026, 9, 30))],
    )
    _add_change(
        url="https://commission.europa.eu/eudr-guidance",
        instrument="Commission EUDR guidance",
        effective_dates=[],
        decision="applies",
        reasons=["You sell leather goods."],
        todos=[("Keep supplier declarations on file", None)],
        tag="eudr",
    )
    _add_change(
        url="https://www.ihk.de/eudr-kmu",
        instrument="IHK guidance — EUDR for small enterprises",
        effective_dates=[],
        decision="unclear",
        reasons=["Do you import the goods yourself or buy from an EU wholesaler?"],
        todos=[],
        tag="eudr",
    )
    html = client.get("/month").text
    heads = [
        "<h2>Already in force</h2>",
        "<h2>Due this month</h2>",
        "<h2>Coming up</h2>",
        "<h2>No fixed date</h2>",
        "<h2>One question decides these</h2>",
    ]
    positions = [html.index(h) for h in heads]
    assert positions == sorted(positions)
    assert all(html.count(h) == 1 for h in heads)
    assert 'due <time datetime="2026-09-30">30 September 2026</time>' in html
    assert 'due <time datetime="2027-01-01">1 January 2027</time>' in html
    assert "pill overdue" not in html[html.index("<h2>Due this month</h2>") :]
    assert "no fixed date — see the obligation" in html
    assert html.count('class="pill overdue"') == 4
    unclear = html[html.index("<h2>One question decides these</h2>") :]
    assert "Do you import the goods yourself or buy from an EU wholesaler?" in unclear
    assert EUDR in unclear
    assert 'href="/profile">Update your profile</a>' in unclear
    assert ">Details</a>" in unclear
    assert 'class="todo' not in unclear
    assert html.count('class="todo') == 7
    assert _subline(html) == "September 2026 · 7 open · 0 done · 1 unclear"
    assert "Nothing due" not in html


# ---------- 3: the pure view builder ----------


def test_month_view_pure_sorting() -> None:
    from regwatch.app import FeedItem, TodoView, _bucket, _month_view, fmt_date_long
    from regwatch.schemas import RegChange

    assert fmt_date_long(date(2026, 6, 19)) == "19 June 2026"
    assert fmt_date_long(date(2027, 1, 1)) == "1 January 2027"
    assert _bucket(None, TODAY) == "no_date"
    assert _bucket(date(2026, 9, 13), TODAY) == "in_force"
    assert _bucket(date(2026, 9, 14), TODAY) == "this_month"
    assert _bucket(date(2026, 9, 30), TODAY) == "this_month"
    assert _bucket(date(2026, 10, 1), TODAY) == "coming_up"

    def todo(text: str, due: date | None, done: bool = False) -> TodoView:
        return TodoView(
            text=text,
            due=due,
            quote="q",
            citation_url="https://x",
            key=text,
            done=done,
            overdue=bool(due and due < TODAY),
        )

    def item(
        id_: int,
        instrument: str,
        decision: str,
        todos: list[TodoView],
        tag: str | None,
        effective: date = date(2026, 12, 24),
    ) -> FeedItem:
        return FeedItem(
            id=id_,
            change=RegChange(
                document_url="https://x",
                instrument=instrument,
                jurisdiction="EU",
                summary="s",
                effective_dates=[effective],
                obligations=[],
            ),
            decision=decision,  # type: ignore[arg-type]
            reasons=[],
            todos=todos,
            created_at=datetime(2026, 7, 1),
            tag=tag,
            source_domain="eur-lex.europa.eu",
            next_due=None,
            in_force=False,
            published_date=None,
            fetched_at=None,
            content_hash="",
            normalize_model="m",
            classify_model="c",
            cost_usd=0.0,
        )

    b = item(
        2,
        "B act",
        "applies",
        [todo("b-late", date(2026, 6, 30)), todo("b-none", None), todo("b-done", None, True)],
        None,
    )
    a = item(
        1, "A act", "applies", [todo("a-late", date(2026, 6, 30))], "withdrawal", date(2026, 11, 11)
    )
    ignored = item(3, "C act", "does_not", [todo("c", date(2026, 9, 20))], None, date(2026, 9, 20))
    m = _month_view([b, ignored, a], TODAY)
    assert [t.view.text for t in m.in_force] == ["a-late", "b-late"]  # due asc, then instrument
    assert [t.view.text for t in m.no_date] == ["b-none"]
    assert [t.view.text for t in m.done] == ["b-done"]
    assert m.this_month == [] and m.coming_up == [] and m.unclear == []
    assert (m.n_open, m.n_done, m.n_unclear) == (3, 1, 0)
    assert m.month_name == "September 2026"
    assert m.nothing_due is False
    assert m.in_force[0].label == WITHDRAWAL and m.in_force[1].label == "B act"
    assert m.in_force[0].change_id == 1
    # effective dates of applies items feed the hint; does_not items (and past dates) do not
    assert m.next_hint == (WITHDRAWAL, date(2026, 11, 11))


# ---------- 4–5: empty states, computed from data ----------


def test_next_hint_from_applies_only(empty_client: TestClient) -> None:
    _add_change(
        url="https://www.ihk.de/eudr-kmu",
        instrument="IHK guidance",
        effective_dates=[date(2026, 10, 1)],  # unclear: must not feed the hint
        decision="unclear",
        reasons=["Do you import yourself?"],
        todos=[],
        tag="eudr",
    )
    html = empty_client.get("/month").text
    assert "<strong>Nothing due this month.</strong>" in html
    assert "Nothing scheduled yet. New changes appear here after the next crawl." in html
    assert "One question decides these" in html
    assert _subline(html) == "September 2026 · 0 open · 0 done · 1 unclear"

    _add_change(
        url="https://www.gesetze-im-internet.de/bgb/__356a.html",
        instrument="§ 356a BGB",
        effective_dates=[date(2027, 3, 1)],
        decision="applies",
        reasons=["Established in Germany."],
        todos=[],
    )
    html = empty_client.get("/month").text
    assert f'Next: {WITHDRAWAL}, <time datetime="2027-03-01">1 March 2027</time>.' in html
    assert "Nothing scheduled yet" not in html

    _add_change(
        url="https://www.bmj.de/widerrufsbutton-faq",
        instrument="BMJ FAQ",
        effective_dates=[],
        decision="applies",
        reasons=["Established in Germany."],
        todos=[("Read the FAQ", date(2026, 12, 1))],
    )
    html = empty_client.get("/month").text
    assert "<strong>Nothing due this month.</strong>" in html
    assert f'Next: {WITHDRAWAL}, <time datetime="2026-12-01">1 December 2026</time>.' in html
    assert "1 March 2027" not in html.split("</div>")[0]
    assert "<h2>Coming up</h2>" in html


def test_empty_profile_copy(empty_client: TestClient) -> None:
    html = empty_client.get("/month").text
    assert "<strong>Nothing due this month.</strong>" in html
    assert "New changes appear here after the next crawl." in html
    assert "<h2>" not in html
    assert _subline(html) == "September 2026 · 0 open · 0 done · 0 unclear"
    assert "Next:" not in html


# ---------- 6–8, 10: check-off ----------


def test_toggle_plain_post_303(client: TestClient) -> None:
    key = _keys(client.get("/month").text)[0]
    r = client.post(f"/todo/{key}/toggle", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/month"
    html = client.get("/month").text
    assert 'class="todo done"' in html
    assert "<h2>Done (1)</h2>" in html
    assert 'aria-pressed="true"' in html and 'aria-label="Mark as not done"' in html


def test_toggle_hx_returns_fragment(client: TestClient) -> None:
    key = _keys(client.get("/month").text)[0]
    r = client.post(f"/todo/{key}/toggle", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<section id="month-list"')
    assert "<html" not in r.text and "htmx.org" not in r.text
    assert "<h2>Done (1)</h2>" in r.text
    assert "3 open · 1 done" in r.text
    assert 'class="todo done"' in r.text


def test_toggle_persists_and_reverts(client: TestClient) -> None:
    import regwatch.app as appmod

    key = _keys(client.get("/month").text)[0]
    client.post(f"/todo/{key}/toggle", follow_redirects=False)
    with session(appmod.engine) as s:
        rows = s.exec(select(TodoStateRow)).all()
    assert [(t.todo_key, t.done) for t in rows] == [(key, True)]
    assert "<h2>Done (1)</h2>" in client.get("/month").text
    client.post(f"/todo/{key}/toggle", headers={"HX-Request": "true"})
    with session(appmod.engine) as s:
        rows = s.exec(select(TodoStateRow)).all()
    assert [(t.todo_key, t.done) for t in rows] == [(key, False)]
    html = client.get("/month").text
    assert "Done (" not in html and 'class="todo done"' not in html
    assert _subline(html) == "September 2026 · 4 open · 0 done · 0 unclear"


def test_all_done_shows_empty_state(client: TestClient) -> None:
    keys = _keys(client.get("/month").text)
    assert len(set(keys)) == 4
    for key in keys:
        client.post(f"/todo/{key}/toggle", follow_redirects=False)
    html = client.get("/month").text
    assert "<strong>Nothing due this month.</strong>" in html
    assert "Nothing scheduled yet. New changes appear here after the next crawl." in html
    assert "<h2>Done (4)</h2>" in html
    assert "Already in force" not in html
    assert html.count('class="todo done"') == 4
    assert _subline(html) == "September 2026 · 0 open · 4 done · 0 unclear"


# ---------- 9, 11: htmx wiring and landing ----------


def test_htmx_script_and_attrs(client: TestClient) -> None:
    html = client.get("/month").text
    assert '<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js" defer></script>' in html
    assert 'hx-post="/todo/' in html
    assert '<section id="month-list" hx-target="#month-list" hx-swap="outerHTML">' in html
    assert html.count('<form method="post" action="/todo/') == 4  # plain form keeps working
    assert "htmx.org" not in client.get("/feed").text


def test_home_redirects_to_month_with_profile(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/month"


def test_month_redirects_to_profile_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appmod = _reload_frozen(tmp_path, monkeypatch, "bare.db")
    c = TestClient(appmod.app)
    assert c.get("/month", follow_redirects=False).headers["location"] == "/profile"
    assert c.post("/todo/abc/toggle", follow_redirects=False).headers["location"] == "/profile"
