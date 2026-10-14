"""RegWatch web app: profile → feed → month view → change detail.

Copy rules (docs/ux-brief.md §3): lead with what to do and by when; legal names second; every
to-do shows its verbatim quote and source link; 'unclear' is a first-class state.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

from regwatch.agent import runner
from regwatch.agent.view import AgentPanel, build_panel
from regwatch.config import (
    CATEGORIES,
    EU_COUNTRIES,
    PLATFORMS,
    REGULATION_LABELS,
    SECTION_SEP,
    SEED_REGULATIONS,
    SOURCE_DOMAINS,
    Settings,
    country_name,
    regulation_instrument,
)
from regwatch.db import (
    AgentRunRow,
    DocumentRow,
    FeedRow,
    ProfileRow,
    RegChangeRow,
    TodoStateRow,
    corpus_counts,
    current_profile,
    feed_row,
    feed_rows,
    last_crawl,
    make_engine,
    session,
    source_domain_for,
    unchecked_changes,
    utcnow,
)
from regwatch.schemas import Decision, RegChange

settings = Settings()
engine = make_engine(settings)
# One process (`regwatch serve`, single worker): agent runs a previous process left queued or
# running can never finish, so they end as failed ("interrupted by restart").
runner.sweep_interrupted(engine)
app = FastAPI(title="RegWatch")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Feed order inside a regulation group: what applies first, then what needs one answer.
DECISION_RANK: dict[str, int] = {"applies": 0, "unclear": 1, "does_not": 2}
# Plain names for the feed's profile subline ("Leather goods", not "leather").
CATEGORY_NAMES: dict[str, str] = dict(CATEGORIES)


@dataclass(frozen=True)
class TodoView:
    text: str
    due: date | None
    quote: str
    citation_url: str
    key: str
    done: bool
    overdue: bool


@dataclass(frozen=True)
class FeedItem:
    id: int
    change: RegChange
    decision: Decision
    reasons: list[str]
    todos: list[TodoView]
    created_at: datetime
    tag: str | None
    source_domain: str
    next_due: date | None
    in_force: bool  # next_due < today
    published_date: str | None
    fetched_at: datetime | None
    content_hash: str
    normalize_model: str
    classify_model: str
    cost_usd: float
    collapsed_line: str = ""  # "N other provisions of <act> don't apply to you"; "" = none


@dataclass(frozen=True)
class FeedGroup:
    key: str
    label: str
    instrument: str | None
    items: list[FeedItem]


@dataclass(frozen=True)
class Footer:
    kind: Literal["seed", "crawl"]
    finished_at: datetime | None
    n_docs: int
    n_sources: int
    tavily_credits: int
    model_cost_usd: float
    n_allowlist: int  # len(SOURCE_DOMAINS)


def fmt_date(d: date | datetime | None) -> str:
    """'19 Jun 2026'; '' for None."""
    if d is None:
        return ""
    return f"{d.day} {d:%b %Y}"


def fmt_stamp(d: datetime | None) -> str:
    """'21 Oct 2026 14:02 UTC'. Stored values are naive UTC — formatted as-is, never converted."""
    if d is None:
        return ""
    return f"{d.day} {d:%b %Y %H:%M} UTC"


MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def fmt_date_long(d: date) -> str:
    """'19 June 2026' — the calm, spelled-out form the month view uses (ux-brief §3.3)."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _today() -> date:
    """The only place app.py asks for today's date; tests monkeypatch `appmod._today`."""
    return date.today()


DECISION_WORDS = {"applies": "applies", "does_not": "doesn't apply", "unclear": "unclear"}


def wrong_call_mailto(change_id: int, decision: str, to: str = "") -> str:
    word = DECISION_WORDS.get(decision, decision)
    return f"mailto:{to}?subject=" + quote(
        f"RegWatch wrong call: change #{change_id} is marked {word}"
    )


templates.env.filters.update(d=fmt_date, stamp=fmt_stamp, dlong=fmt_date_long)


VISITOR_COOKIE = "rw_visitor"
VISITOR_MAX_AGE = 60 * 60 * 24 * 30
PROFILE_LOCKED_NOTE = (
    "This demo shares one store profile with every visitor, so it can't be changed here."
)


def _same_origin(request: Request) -> bool:
    """htmx's HX-Request header (a cross-site page cannot send it without a CORS preflight we
    never answer) or an Origin / Referer on this host. Guards every agent POST."""
    if request.headers.get("HX-Request") == "true":
        return True
    host = request.url.netloc
    for name in ("origin", "referer"):
        value = request.headers.get(name)
        if value:
            return urlsplit(value).netloc == host
    return False


def _get_run(run_id: int | None) -> AgentRunRow | None:
    if run_id is None:
        return None
    with session(engine) as s:
        return s.get(AgentRunRow, run_id)


def _latest_run(visitor: str | None, change_id: int, profile_id: int) -> AgentRunRow | None:
    """This visitor's newest run for (profile, change); runs of an older profile are ignored."""
    if not visitor:
        return None
    with session(engine) as s:
        return s.exec(
            select(AgentRunRow)
            .where(
                AgentRunRow.visitor_id == visitor,
                AgentRunRow.regchange_id == change_id,
                AgentRunRow.profile_id == profile_id,
            )
            .order_by(col(AgentRunRow.id).desc())
        ).first()


def _visitor_run(request: Request, run_id: int) -> tuple[AgentRunRow, RegChange, str] | None:
    """(run, its change, source domain) when the cookie's visitor owns the run and the run was
    made for the current profile, else None. Every /agent/{run_id} route goes through this."""
    visitor = request.cookies.get(VISITOR_COOKIE)
    with session(engine) as s:
        run = s.get(AgentRunRow, run_id)
        if run is None or not visitor or run.visitor_id != visitor:
            return None
        prof = current_profile(s)
        if prof is None or prof.id != run.profile_id:
            return None
        ch = s.get(RegChangeRow, run.regchange_id)
        if ch is None:
            return None
        doc = s.get(DocumentRow, ch.document_url)
        return run, ch.to_schema(), source_domain_for(ch.document_url, doc)


def _panel(
    run: AgentRunRow | None,
    change_id: int,
    change: RegChange,
    source_domain: str,
    *,
    message: str = "",
) -> AgentPanel:
    """The agent card for this change; a waiting run past its 30 minutes is expired first."""
    now = utcnow()
    if run is not None and run.id is not None and run.status == "waiting":
        if runner.expire_if_stale(engine, run.id, now=now):
            run = _get_run(run.id)
    return build_panel(
        engine,
        settings,
        change_id=change_id,
        change=change,
        source_domain=source_domain,
        run=run,
        now=now,
        message=message,
    )


def _fragment(request: Request, panel: AgentPanel) -> Response:
    resp = templates.TemplateResponse(request, "partials/agent_panel.html", {"a": panel})
    if panel.status == "done":
        resp.headers["HX-Refresh"] = "true"  # the result card sits above the original: reload once
    return resp


def _todo_key(quote: str, url: str) -> str:
    return hashlib.sha1(f"{url}|{quote}".encode()).hexdigest()[:16]


def _done_keys(s: Session, profile_id: int) -> set[str]:
    return {
        t.todo_key
        for t in s.exec(select(TodoStateRow).where(TodoStateRow.profile_id == profile_id)).all()
        if t.done
    }


def _item(row: FeedRow, done_keys: set[str], today: date) -> FeedItem:
    todos = [
        TodoView(
            text=t.text,
            due=t.due,
            quote=t.quote,
            citation_url=t.citation_url,
            key=_todo_key(t.quote, t.citation_url),
            done=_todo_key(t.quote, t.citation_url) in done_keys,
            overdue=bool(t.due and t.due < today),
        )
        for t in row.appl.todos
    ]
    dues = [t.due for t in todos if t.due is not None]
    next_due = min(dues) if dues else None
    return FeedItem(
        id=row.change_id,
        change=row.change,
        decision=row.appl.decision,
        reasons=row.appl.reasons,
        todos=todos,
        created_at=row.created_at,
        tag=row.tag,
        source_domain=row.source_domain,
        next_due=next_due,
        in_force=bool(next_due and next_due < today),
        published_date=row.published_date,
        fetched_at=row.fetched_at,
        content_hash=row.content_hash,
        normalize_model=row.normalize_model,
        classify_model=row.classify_model,
        cost_usd=row.cost_usd,
    )


def _act_name(instrument: str) -> str:
    return instrument.split(SECTION_SEP)[0]


def _collapse_line(act: str, n: int) -> str:
    if n == 1:
        return f"1 other provision of {act} doesn't apply to you"
    return f"{n} other provisions of {act} don't apply to you"


def _collapse_sections(ordered: list[FeedItem]) -> list[FeedItem]:
    """Keep applies/unclear sections of a multi-change act; fold its does_not sections into one
    count line on the act's last shown item. All-does_not acts keep one item (the rest counted).
    Sections of one act are the changes of one document version: (document_url, content_hash),
    so an older version of the same URL is never counted as a provision of the newer one."""
    by_url: dict[tuple[str, str], list[FeedItem]] = {}
    for it in ordered:
        by_url.setdefault((it.change.document_url, it.content_hash), []).append(it)
    drop: set[int] = set()
    note: dict[int, str] = {}
    for its in by_url.values():
        hidden = [i for i in its if i.decision == "does_not"]
        if len(its) < 2 or not hidden:
            continue
        shown = [i for i in its if i.decision != "does_not"]
        if not shown:
            shown, hidden = hidden[:1], hidden[1:]
        act = _act_name(shown[-1].change.instrument)
        drop.update(i.id for i in hidden)
        note[shown[-1].id] = _collapse_line(act, len(hidden))
    return [replace(i, collapsed_line=note.get(i.id, "")) for i in ordered if i.id not in drop]


def _group(items: list[FeedItem]) -> list[FeedGroup]:
    """Tagged groups in SEED_REGULATIONS order, then untagged groups (keyed by instrument) by
    newest item; inside a group: applies → unclear → does_not, newest first."""
    by_key: dict[str, list[FeedItem]] = {}
    for it in items:
        by_key.setdefault(it.tag or _act_name(it.change.instrument), []).append(it)
    seed_tags = [r["tag"] for r in SEED_REGULATIONS]
    tagged = [t for t in seed_tags if t in by_key]
    untagged = sorted(
        (k for k in by_key if k not in seed_tags),
        key=lambda k: max(i.created_at for i in by_key[k]),
        reverse=True,
    )
    groups: list[FeedGroup] = []
    for key in tagged + untagged:
        ordered = sorted(by_key[key], key=lambda i: i.created_at, reverse=True)
        ordered.sort(key=lambda i: DECISION_RANK.get(i.decision, len(DECISION_RANK)))
        ordered = _collapse_sections(ordered)
        if key in seed_tags:
            groups.append(
                FeedGroup(
                    key=key,
                    label=REGULATION_LABELS.get(key, key),
                    instrument=regulation_instrument(key),
                    items=ordered,
                )
            )
        else:
            groups.append(FeedGroup(key=key, label=key, instrument=None, items=ordered))
    return groups


def _footer(s: Session, rows: list[FeedRow]) -> Footer:
    """'crawl' once a finished CrawlRunRow exists; before that, the recorded seed run's numbers."""
    run = last_crawl(s)
    n_docs, n_sources = corpus_counts(s)
    if run is not None and run.finished_at is not None:
        return Footer(
            kind="crawl",
            finished_at=run.finished_at,
            n_docs=n_docs,
            n_sources=n_sources,
            tavily_credits=run.tavily_credits,
            model_cost_usd=run.model_cost_usd,
            n_allowlist=len(SOURCE_DOMAINS),
        )
    return Footer(
        kind="seed",
        finished_at=None,
        n_docs=n_docs,
        n_sources=n_sources,
        tavily_credits=sum(r.tavily_credits for r in rows),
        model_cost_usd=sum(r.cost_usd for r in rows),
        n_allowlist=len(SOURCE_DOMAINS),
    )


Bucket = Literal["in_force", "this_month", "coming_up", "no_date"]


def _bucket(due: date | None, today: date) -> Bucket:
    if due is None:
        return "no_date"
    if due < today:
        return "in_force"
    if (due.year, due.month) == (today.year, today.month):
        return "this_month"
    return "coming_up"


@dataclass(frozen=True)
class MonthTodo:
    view: TodoView
    change_id: int
    instrument: str
    label: str
    bucket: Bucket


@dataclass(frozen=True)
class UnclearItem:
    change_id: int
    instrument: str
    label: str
    question: str  # reasons[0]


@dataclass(frozen=True)
class MonthView:
    today: date
    month_name: str  # "September 2026"
    in_force: list[MonthTodo]
    this_month: list[MonthTodo]
    coming_up: list[MonthTodo]
    no_date: list[MonthTodo]
    done: list[MonthTodo]
    unclear: list[UnclearItem]
    n_open: int
    n_done: int
    n_unclear: int
    nothing_due: bool  # no open item in force or due this month
    next_hint: tuple[str, date] | None  # (label, date): earliest date >= today


UNCLEAR_FALLBACK = "We couldn't tell from your profile."


def _month_view(items: list[FeedItem], today: date) -> MonthView:
    """Pure: bucket the open to-dos of `applies` items by due date; done to-dos go to `done`
    whatever their bucket; `unclear` items become one-question entries. Nothing is invented:
    the next hint is the earliest stored due date or effective date on or after `today`."""
    buckets: dict[Bucket, list[MonthTodo]] = {
        "in_force": [],
        "this_month": [],
        "coming_up": [],
        "no_date": [],
    }
    done: list[MonthTodo] = []
    unclear: list[UnclearItem] = []
    candidates: list[tuple[date, str]] = []
    for it in items:
        label = REGULATION_LABELS.get(it.tag or "", it.change.instrument)
        if it.decision == "unclear":
            unclear.append(
                UnclearItem(
                    change_id=it.id,
                    instrument=it.change.instrument,
                    label=label,
                    question=it.reasons[0] if it.reasons else UNCLEAR_FALLBACK,
                )
            )
            continue
        if it.decision != "applies":
            continue
        candidates.extend((d, label) for d in it.change.effective_dates if d >= today)
        for t in it.todos:
            mt = MonthTodo(
                view=t,
                change_id=it.id,
                instrument=it.change.instrument,
                label=label,
                bucket=_bucket(t.due, today),
            )
            if t.done:
                done.append(mt)
                continue
            buckets[mt.bucket].append(mt)
            if t.due is not None and t.due >= today:
                candidates.append((t.due, label))

    def order(m: MonthTodo) -> tuple[date, str, str]:
        return (m.view.due or date.max, m.instrument, m.view.text)

    for lst in buckets.values():
        lst.sort(key=order)
    done.sort(key=order)
    unclear.sort(key=lambda u: u.instrument)
    n_open = sum(len(lst) for lst in buckets.values())
    nearest = min(candidates) if candidates else None
    return MonthView(
        today=today,
        month_name=f"{MONTHS[today.month - 1]} {today.year}",
        in_force=buckets["in_force"],
        this_month=buckets["this_month"],
        coming_up=buckets["coming_up"],
        no_date=buckets["no_date"],
        done=done,
        unclear=unclear,
        n_open=n_open,
        n_done=len(done),
        n_unclear=len(unclear),
        nothing_due=not buckets["in_force"] and not buckets["this_month"],
        next_hint=(nearest[1], nearest[0]) if nearest else None,
    )


def _load_month(profile_id: int, today: date) -> MonthView:
    with session(engine) as s:
        rows = feed_rows(s, profile_id)
        done_keys = _done_keys(s, profile_id)
    return _month_view([_item(r, done_keys, today) for r in rows], today)


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Any:
    with session(engine) as s:
        prof = current_profile(s)
    if prof is None:
        return RedirectResponse("/profile", status_code=303)
    return RedirectResponse("/month", status_code=303)


@app.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request) -> Any:
    with session(engine) as s:
        prof = current_profile(s)
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "countries": EU_COUNTRIES,
            "categories": CATEGORIES,
            "platforms": PLATFORMS,
            "profile": prof.to_schema() if prof else None,
            "locked": settings.profile_locked,
        },
    )


@app.post("/profile")
def profile_save(
    establishment_country: str = Form(...),
    ship_to: list[str] = Form(default=[]),
    categories: list[str] = Form(default=[]),
    platform: str = Form("shopify"),
) -> Any:
    if settings.profile_locked:
        return HTMLResponse(PROFILE_LOCKED_NOTE, status_code=403)
    if "ALL_EU" in ship_to:
        ship_to = list(EU_COUNTRIES)
    with session(engine) as s:
        s.add(
            ProfileRow(
                establishment_country=establishment_country.upper(),
                ship_to=json.dumps(sorted(set(ship_to))),
                categories=json.dumps(sorted(set(categories))),
                platform=platform,
            )
        )
        s.commit()
    return RedirectResponse("/feed", status_code=303)


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request) -> Any:
    today = _today()
    with session(engine) as s:
        prof = current_profile(s)
        if prof is None or prof.id is None:
            return RedirectResponse("/profile", status_code=303)
        rows = feed_rows(s, prof.id)
        done_keys = _done_keys(s, prof.id)
        unchecked = unchecked_changes(s, prof.id)
        footer = _footer(s, rows)
    groups = _group([_item(r, done_keys, today) for r in rows])
    profile = prof.to_schema()
    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "groups": groups,
            "unchecked": unchecked,
            "f": footer,
            "profile": profile,
            "establishment": country_name(prof.establishment_country),
            "category_names": [CATEGORY_NAMES.get(c, c) for c in profile.categories],
        },
    )


@app.get("/month", response_class=HTMLResponse)
def month(request: Request) -> Any:
    with session(engine) as s:
        prof = current_profile(s)
    if prof is None or prof.id is None:
        return RedirectResponse("/profile", status_code=303)
    m = _load_month(prof.id, _today())
    return templates.TemplateResponse(request, "month.html", {"m": m})


@app.post("/todo/{key}/toggle")
def toggle_todo(request: Request, key: str) -> Any:
    """Flip one to-do's done mark. htmx callers (HX-Request: true) get the refreshed
    #month-list fragment; a plain form post is redirected back to /month."""
    with session(engine) as s:
        prof = current_profile(s)
        if prof is None or prof.id is None:
            return RedirectResponse("/profile", status_code=303)
        row = s.exec(
            select(TodoStateRow).where(
                TodoStateRow.profile_id == prof.id, TodoStateRow.todo_key == key
            )
        ).first()
        if row is None:
            s.add(TodoStateRow(profile_id=prof.id, todo_key=key, done=True))
        else:
            row.done = not row.done
            s.add(row)
        s.commit()
        profile_id = prof.id
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "partials/month_list.html", {"m": _load_month(profile_id, _today())}
        )
    return RedirectResponse("/month", status_code=303)


@app.get("/change/{change_id}", response_class=HTMLResponse)
def change_detail(request: Request, change_id: int) -> Any:
    """Stored rows only: nothing is classified from a web route."""
    today = _today()
    with session(engine) as s:
        prof = current_profile(s)
        if prof is None or prof.id is None:
            return RedirectResponse("/profile", status_code=303)
        row = feed_row(s, prof.id, change_id)
        if row is not None:
            item = _item(row, _done_keys(s, prof.id), today)
            ctx: dict[str, Any] = {
                "item": item,
                "change": item.change,
                "created_at": item.created_at,
                "source_domain": item.source_domain,
                "published_date": item.published_date,
                "fetched_at": item.fetched_at,
                "content_hash": item.content_hash,
                "normalize_model": item.normalize_model,
                "mailto": wrong_call_mailto(change_id, item.decision, settings.report_email),
            }
        else:
            ch = s.get(RegChangeRow, change_id)
            if ch is None:
                return HTMLResponse("Not found", status_code=404)
            doc = s.get(DocumentRow, ch.document_url)
            ctx = {
                "item": None,
                "change": ch.to_schema(),
                "created_at": ch.created_at,
                "source_domain": source_domain_for(ch.document_url, doc),
                "published_date": doc.published_date if doc is not None else None,
                "fetched_at": doc.fetched_at if doc is not None else None,
                "content_hash": ch.content_hash,
                "normalize_model": ch.model,
                "mailto": "",
            }
    ctx["change_id"] = change_id
    ctx["profile"] = prof.to_schema()
    ctx["a"] = None
    shown = ctx["item"]
    if settings.agent_enabled and shown is not None and shown.decision == "unclear":
        assert prof.id is not None
        run = _latest_run(request.cookies.get(VISITOR_COOKIE), change_id, prof.id)
        ctx["a"] = _panel(run, change_id, shown.change, shown.source_domain)
    return templates.TemplateResponse(request, "change_detail.html", ctx)


@app.post("/change/{change_id}/agent")
def agent_start(request: Request, change_id: int) -> Any:
    """Start (or rejoin) this visitor's agent run on an unclear change."""
    if not settings.agent_enabled:
        return HTMLResponse("Not found", status_code=404)
    if not _same_origin(request):
        return HTMLResponse("Forbidden", status_code=403)
    with session(engine) as s:
        prof = current_profile(s)
        profile_id = prof.id if prof is not None else None
        row = feed_row(s, profile_id, change_id) if profile_id is not None else None
    if profile_id is None or row is None or row.appl.decision != "unclear":
        return HTMLResponse("Not found", status_code=404)
    known = request.cookies.get(VISITOR_COOKIE)
    visitor = known or secrets.token_hex(16)
    run_id, refusal = runner.start_run(
        engine,
        settings,
        visitor_id=visitor,
        profile_id=profile_id,
        regchange_id=change_id,
        now=utcnow(),
    )
    resp: Response
    if request.headers.get("HX-Request") == "true":
        panel = _panel(_get_run(run_id), change_id, row.change, row.source_domain, message=refusal)
        resp = _fragment(request, panel)
    elif refusal:
        resp = HTMLResponse(refusal, status_code=429)
    else:
        resp = RedirectResponse(f"/change/{change_id}", status_code=303)
    if not known:
        resp.set_cookie(
            VISITOR_COOKIE, visitor, max_age=VISITOR_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


@app.get("/agent/{run_id}", response_class=HTMLResponse)
def agent_trace(request: Request, run_id: int) -> Any:
    """The trace fragment htmx polls; only for the run's visitor and the current profile."""
    if not settings.agent_enabled:
        return HTMLResponse("Not found", status_code=404)
    found = _visitor_run(request, run_id)
    if found is None:
        return HTMLResponse("Not found", status_code=404)
    run, change, source_domain = found
    return _fragment(request, _panel(run, run.regchange_id, change, source_domain))


@app.post("/agent/{run_id}/answer")
def agent_answer(request: Request, run_id: int, option: str = Form("")) -> Any:
    """The visitor's pick for the open question, by option index only (never free text)."""
    if not settings.agent_enabled:
        return HTMLResponse("Not found", status_code=404)
    if not _same_origin(request):
        return HTMLResponse("Forbidden", status_code=403)
    found = _visitor_run(request, run_id)
    if found is None:
        return HTMLResponse("Not found", status_code=404)
    run, change, source_domain = found
    if not (option.isascii() and option.isdigit()):
        return HTMLResponse("Pick one of the options.", status_code=400)
    if run.status == "waiting" and runner.expire_if_stale(engine, run_id, now=utcnow()):
        return HTMLResponse("This question has closed.", status_code=409)
    result = runner.answer(engine, settings, run_id, int(option))
    if result == "bad_option":
        return HTMLResponse("Pick one of the options.", status_code=400)
    if result == "not_waiting":
        return HTMLResponse("This question has closed.", status_code=409)
    if request.headers.get("HX-Request") == "true":
        panel = _panel(_get_run(run_id), run.regchange_id, change, source_domain)
        return _fragment(request, panel)
    return RedirectResponse(f"/change/{run.regchange_id}", status_code=303)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
