"""RegWatch web app: profile → feed → month view → change detail.

Copy rules (docs/ux-brief.md §3): lead with what to do and by when; legal names second; every
to-do shows its verbatim quote and source link; 'unclear' is a first-class state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from regwatch.config import (
    CATEGORIES,
    EU_COUNTRIES,
    PLATFORMS,
    REGULATION_LABELS,
    SEED_REGULATIONS,
    SOURCE_DOMAINS,
    Settings,
    country_name,
    regulation_instrument,
)
from regwatch.db import (
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
)
from regwatch.schemas import Decision, RegChange

settings = Settings()
engine = make_engine(settings)
app = FastAPI(title="RegWatch")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Feed order inside a regulation group: what applies first, then what needs one answer.
DECISION_RANK: dict[str, int] = {"applies": 0, "unclear": 1, "does_not": 2}


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


def wrong_call_mailto(change_id: int, decision: str, to: str = "") -> str:
    return f"mailto:{to}?subject=" + quote(
        f"RegWatch wrong call: change #{change_id} is marked {decision}"
    )


templates.env.filters.update(d=fmt_date, stamp=fmt_stamp, dlong=fmt_date_long)


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


def _group(items: list[FeedItem]) -> list[FeedGroup]:
    """Tagged groups in SEED_REGULATIONS order, then untagged groups (keyed by instrument) by
    newest item; inside a group: applies → unclear → does_not, newest first."""
    by_key: dict[str, list[FeedItem]] = {}
    for it in items:
        by_key.setdefault(it.tag or it.change.instrument, []).append(it)
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


def _feed_items(profile_id: int) -> list[dict[str, Any]]:
    """Dict shape kept for month(); built on feed_rows() + _item()."""
    today = _today()
    with session(engine) as s:
        rows = feed_rows(s, profile_id)
        done_keys = _done_keys(s, profile_id)
    items: list[dict[str, Any]] = []
    for row in rows:
        it = _item(row, done_keys, today)
        items.append(
            {
                "id": it.id,
                "change": it.change,
                "decision": it.decision,
                "reasons": it.reasons,
                "todos": [asdict(t) for t in it.todos],
                "created_at": it.created_at,
                "credits": row.tavily_credits,
            }
        )
    return items


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
        },
    )


@app.post("/profile")
def profile_save(
    establishment_country: str = Form(...),
    ship_to: list[str] = Form(default=[]),
    categories: list[str] = Form(default=[]),
    platform: str = Form("shopify"),
) -> Any:
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
    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "groups": groups,
            "unchecked": unchecked,
            "f": footer,
            "profile": prof.to_schema(),
            "establishment": country_name(prof.establishment_country),
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
    return templates.TemplateResponse(request, "change_detail.html", ctx)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
