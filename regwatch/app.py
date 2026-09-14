"""RegWatch web app: profile → feed → month view → change detail.

Copy rules (docs/ux-brief.md §3): lead with what to do and by when; legal names second; every
to-do shows its verbatim quote and source link; 'unclear' is a first-class state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import col, select

from regwatch.config import CATEGORIES, EU_COUNTRIES, PLATFORMS, Settings
from regwatch.db import (
    ApplicabilityRow,
    ProfileRow,
    RegChangeRow,
    TodoStateRow,
    current_profile,
    make_engine,
    session,
)

settings = Settings()
engine = make_engine(settings)
app = FastAPI(title="RegWatch")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _todo_key(quote: str, url: str) -> str:
    return hashlib.sha1(f"{url}|{quote}".encode()).hexdigest()[:16]


def _feed_items(profile_id: int) -> list[dict[str, Any]]:
    with session(engine) as s:
        rows = s.exec(
            select(ApplicabilityRow, RegChangeRow)
            .where(ApplicabilityRow.regchange_id == RegChangeRow.id)
            .where(ApplicabilityRow.profile_id == profile_id)
            .order_by(col(RegChangeRow.created_at).desc())
        ).all()
        done_keys = {
            t.todo_key
            for t in s.exec(select(TodoStateRow).where(TodoStateRow.profile_id == profile_id)).all()
            if t.done
        }
    items: list[dict[str, Any]] = []
    for app_row, ch_row in rows:
        change = ch_row.to_schema()
        appl = app_row.to_schema()
        todos = [
            {
                **t.model_dump(),
                "key": _todo_key(t.quote, t.citation_url),
                "done": _todo_key(t.quote, t.citation_url) in done_keys,
            }
            for t in appl.todos
        ]
        items.append(
            {
                "id": ch_row.id,
                "change": change,
                "decision": appl.decision,
                "reasons": appl.reasons,
                "todos": todos,
                "created_at": ch_row.created_at,
                "credits": app_row.tavily_credits,
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
    return templates.TemplateResponse(
        request,
        "feed.html",
        {"items": items, "profile": prof.to_schema(), "credits": sum(i["credits"] for i in items)},
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
