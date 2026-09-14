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
    regulation_instrument,
)
from regwatch.db import (
    DocumentRow,
    FeedRow,
    ProfileRow,
    TodoStateRow,
    corpus_counts,
    current_profile,
    feed_rows,
    last_crawl,
    make_engine,
    session,
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


def wrong_call_mailto(change_id: int, decision: str, to: str = "") -> str:
    return f"mailto:{to}?subject=" + quote(
        f"RegWatch wrong call: change #{change_id} is marked {decision}"
    )


templates.env.filters.update(d=fmt_date, stamp=fmt_stamp)


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
    today = date.today()
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Any:
    with session(engine) as s:
        prof = current_profile(s)
    if prof is None:
        return RedirectResponse("/profile", status_code=303)
    return RedirectResponse("/feed", status_code=303)


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
    with session(engine) as s:
        prof = current_profile(s)
    if prof is None or prof.id is None:
        return RedirectResponse("/profile", status_code=303)
    items = _feed_items(prof.id)
    with session(engine) as s:
        run = last_crawl(s)
        domains = s.exec(select(DocumentRow.source_domain).distinct()).all()
    n_sources = len(domains) or len(SOURCE_DOMAINS)
    credits = run.tavily_credits if run is not None else sum(i["credits"] for i in items)
    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "items": items,
            "profile": prof.to_schema(),
            "credits": credits,
            "last_crawl": run,
            "n_sources": n_sources,
        },
    )


@app.get("/month", response_class=HTMLResponse)
def month(request: Request) -> Any:
    with session(engine) as s:
        prof = current_profile(s)
    if prof is None or prof.id is None:
        return RedirectResponse("/profile", status_code=303)
    today = date.today()
    todos: list[dict[str, Any]] = []
    for item in _feed_items(prof.id):
        if item["decision"] != "applies":
            continue
        for t in item["todos"]:
            due = date.fromisoformat(t["due"]) if isinstance(t["due"], str) else t["due"]
            todos.append(
                {
                    **t,
                    "due": due,
                    "instrument": item["change"].instrument,
                    "change_id": item["id"],
                    "overdue": bool(due and due < today),
                }
            )
    todos.sort(key=lambda t: (t["done"], t["due"] or date.max))
    return templates.TemplateResponse(request, "month.html", {"todos": todos, "today": today})


@app.post("/todo/{key}/toggle")
def toggle_todo(key: str) -> Any:
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
    return RedirectResponse("/month", status_code=303)


@app.get("/change/{change_id}", response_class=HTMLResponse)
def change_detail(request: Request, change_id: int) -> Any:
    with session(engine) as s:
        prof = current_profile(s)
    if prof is None or prof.id is None:
        return RedirectResponse("/profile", status_code=303)
    item = next((i for i in _feed_items(prof.id) if i["id"] == change_id), None)
    if item is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request, "change_detail.html", {"item": item, "profile": prof.to_schema()}
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
