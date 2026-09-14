<!-- fmt: off -->
# Oct 22 plan — Month view polish: buckets by due date, overdue-first, "mark done" via htmx, empty/unclear states

Orchestrator notes R1–R5 (override the plan where they conflict):
- R1: Oct 21 already computes `TodoView.overdue` and `_done_keys`; reuse. `_feed_items` is only used by `month()` — after you move `month()` onto `feed_rows()`+`_item()`, delete `_feed_items` if `grep -rn _feed_items regwatch tests` shows no other user.
- R2: `_today()` helper: the ONLY call site of `date.today()` in app.py (Oct 21's `feed()`/`change_detail()` may call `date.today()` — route them through `_today()` too; tests monkeypatch `appmod._today` on the reloaded module).
- R3: `/` landing: change `home()` to redirect to `/month` when a profile exists (Oct 23 planner's note; judges land on the actionable page). Update `test_profile_redirect_when_empty` only if it asserts the `/feed` target (it asserts `/profile` on empty — leave it); add one assertion in tests/test_month.py that `/` → 303 `/month` with a profile.
- R4: Two tiny feed.html fixes from the Oct 21 verifier (allowed because you own templates this wave): (a) profile subline shows category plain names via `dict(CATEGORIES)` mapping (pass `category_names` in the feed context or a filter); (b) omit `<p class="law">` when `g.items|length == 1` and the single item's `change.instrument` already contains the group instrument's leading token (simplest: omit when the group has exactly one item). Add one assertion each in tests/test_pages.py (append-only; keep existing tests passing).
- R5: month view "label" for a to-do = `REGULATION_LABELS.get(tag, change.instrument)`; the link goes to `/change/<id>`; the `source` link to `citation_url` must be present on every to-do row.

Non-negotiables (CLAUDE.md + UX §3.3):
- Month view: to-dos sorted by due; overdue first with calm factual label "in force since 19 June 2026"; check-off persists; empty state "Nothing due this month. Next: <label>, <date>." computed from data; unclear surfaced as "one question decides these"; mobile first; red only for genuinely overdue (`.pill.overdue` only in the in_force bucket).
- Templates display stored data only; no invented text/dates. No changes to `schemas.py`, `db.py`, `ingest/*`, `understand/*`, `jobs/crawl.py`, `cli.py`, `config.py`, `examples/*`, `tests/fixtures/*`.
- htmx from pinned CDN (`https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`, `defer`) on month.html only; the plain `<form method="post">` must keep working without JS; toggle returns the `#month-list` fragment on `HX-Request: true`, 303 otherwise.
- `tests/test_app.py::test_month_view_marks_overdue_and_toggles`: change ONLY the one assertion `"in force since 2026-06-19"` → `'in force since <time datetime="2026-06-19">19 June 2026</time>'` with a comment citing ux-brief §3.3; everything else in that file unchanged.

Allowed files: `regwatch/app.py`, `regwatch/templates/month.html`, `regwatch/templates/partials/month_list.html` (new), `regwatch/templates/base.html` (CSS + `{% block head %}`), `regwatch/templates/feed.html` (R4 only), `tests/test_month.py` (new), `tests/test_app.py` (one assertion), `tests/test_pages.py` (append-only), `docs/plans/oct22.md`, `docs/issues.md`, `README.md` (status line). Forbidden: everything else.

## 1. Files
**Touch:** `regwatch/app.py` (month view model, `month()`, `toggle_todo()`, `_today`, `fmt_date_long`, delete `_feed_items`) · `regwatch/templates/month.html` (rewrite) · `regwatch/templates/partials/month_list.html` (new) · `regwatch/templates/base.html` (add `{% block head %}` + ~6 CSS rules) · `tests/test_month.py` (new) · `tests/test_app.py` (one assertion) · `docs/issues.md` · `docs/plans/oct22.md`.

## 2. Signatures (app.py)
```python
MONTHS = ("January", …, "December")                      # fixed English
def fmt_date_long(d: date) -> str: return f"{d.day} {MONTHS[d.month-1]} {d.year}"   # "19 June 2026"; Jinja filter "dlong"
def _today() -> date: return date.today()                 # only place date.today() is called; tests monkeypatch appmod._today
Bucket = Literal["in_force", "this_month", "coming_up", "no_date"]
def _bucket(due: date | None, today: date) -> Bucket      # None→no_date; due<today→in_force; same (y,m)→this_month; else coming_up
@dataclass(frozen=True) class MonthTodo: view: TodoView; change_id: int; instrument: str; label: str; bucket: Bucket
@dataclass(frozen=True) class UnclearItem: change_id: int; instrument: str; label: str; question: str   # question = reasons[0]
@dataclass(frozen=True) class MonthView:
    today: date; month_name: str                          # "September 2026"
    in_force: list[MonthTodo]; this_month: list[MonthTodo]; coming_up: list[MonthTodo]; no_date: list[MonthTodo]
    done: list[MonthTodo]; unclear: list[UnclearItem]
    n_open: int; n_done: int; n_unclear: int
    nothing_due: bool                                     # not in_force and not this_month (open items only)
    next_hint: tuple[str, date] | None                    # (label, date): earliest date >= today
def _month_view(items: list[FeedItem], today: date) -> MonthView   # PURE
def _load_month(profile_id: int, today: date) -> MonthView         # feed_rows(s, pid) → _item(row, done_keys, today) → _month_view
@app.get("/month")  def month(request: Request) -> Any             # 303 /profile if no profile; else month.html {"m": m}
@app.post("/todo/{key}/toggle") def toggle_todo(request: Request, key: str) -> Any
```
Rules inside `_month_view`: only `decision == "applies"` items yield MonthTodos; `label = REGULATION_LABELS.get(item.tag or "", item.change.instrument)`; done items go to `done` regardless of bucket; sort every list by `(due or date.max, instrument, text)`. `next_hint = min` over `{(t.due, label) for open todos with due >= today} ∪ {(d, label) for d in change.effective_dates if d >= today}` of applies items, or None. Unclear items: `decision == "unclear"`, `question = reasons[0] if reasons else "We couldn't tell from your profile."`. `n_open = len(in_force+this_month+coming_up+no_date)`.
Toggle: keep the DB logic; after commit, `if request.headers.get("HX-Request") == "true": return TemplateResponse(request, "partials/month_list.html", {"m": _load_month(prof.id, _today())})` (200) else `RedirectResponse("/month", 303)`.

## 3. Markup and copy
`month.html`:
```html
{% extends "base.html" %}{% block title %}This month · RegWatch{% endblock %}
{% block head %}<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js" defer></script>{% endblock %}
{% block body %}<h1>What to do this month</h1>{% include "partials/month_list.html" %}{% endblock %}
```
`partials/month_list.html` (first char is `<section`; whole thing is the htmx swap unit):
```html
{% macro todo(t) %}
<div class="todo{% if t.view.done %} done{% endif %}">
  <form method="post" action="/todo/{{t.view.key}}/toggle" hx-post="/todo/{{t.view.key}}/toggle"><button type="submit" aria-pressed="{{'true' if t.view.done else 'false'}}" aria-label="{{'Mark as not done' if t.view.done else 'Mark as done'}}">{% if t.view.done %}✓{% endif %}</button></form>
  <div class="body"><div class="text">{{t.view.text}}</div>
  <div class="meta">{% if t.bucket=="in_force" %}<span class="pill overdue">in force since <time datetime="{{t.view.due.isoformat()}}">{{t.view.due|dlong}}</time></span>{% elif t.view.due %}due <time datetime="{{t.view.due.isoformat()}}">{{t.view.due|dlong}}</time>{% else %}no fixed date — see the obligation{% endif %}
  · <a href="/change/{{t.change_id}}">{{t.label}}</a> · <a href="{{t.view.citation_url}}" rel="noopener">source</a></div></div>
</div>
{% endmacro %}
<section id="month-list" hx-target="#month-list" hx-swap="outerHTML">
<p class="muted">{{m.month_name}} · {{m.n_open}} open · {{m.n_done}} done · {{m.n_unclear}} unclear</p>
{% if m.nothing_due %}<div class="card empty"><strong>Nothing due this month.</strong>
{% if m.next_hint %}Next: {{m.next_hint[0]}}, <time datetime="{{m.next_hint[1].isoformat()}}">{{m.next_hint[1]|dlong}}</time>.{% else %}Nothing scheduled yet. New changes appear here after the next crawl.{% endif %}</div>{% endif %}
{% for heading, hint, todos in [("Already in force","The rule already applies — do these now.",m.in_force),("Due this month","",m.this_month),("Coming up","",m.coming_up),("No fixed date","",m.no_date)] if todos %}
<h2>{{heading}}</h2>{% if hint %}<p class="hint">{{hint}}</p>{% endif %}<div class="card">
{% for t in todos %}{{ todo(t) }}{% endfor %}</div>{% endfor %}
{% if m.unclear %}<h2>One question decides these</h2><div class="card">
{% for u in m.unclear %}<div class="question"><div class="text">{{u.label}}</div><div class="muted">{{u.question}}</div>
<div class="meta"><a href="/profile">Update your profile</a> · <a href="/change/{{u.change_id}}">Details</a></div></div>{% endfor %}</div>{% endif %}
{% if m.done %}<h2>Done ({{m.done|length}})</h2><div class="card">{% for t in m.done %}{{ todo(t) }}{% endfor %}</div>{% endif %}
</section>
```
(The macro must render exactly `class="todo done"` for the done state so test_app's assertion holds. Note: with the macro defined before `<section`, the rendered fragment starts with a newline — strip leading whitespace in the template so the fragment's first non-space char is `<section`, and have the test use `.lstrip()`.)

## 4. CSS additions (base.html)
```css
.hint{font-size:14px;color:var(--ink3);margin:-4px 0 8px}
.todo .body{flex:1;min-width:0}.todo time{white-space:nowrap}
.todo button[aria-pressed="true"]{background:var(--applies-bg);border-color:var(--applies);color:var(--applies)}
.todo.done .meta,.todo.done .pill.overdue{opacity:.55}
.question{padding:10px 0;border-top:1px solid var(--line)}.question:first-child{border-top:0}
.card.empty strong{display:block;margin-bottom:4px}
```
Add `{% block head %}{% endblock %}` inside `<head>` in base.html.

## 5. htmx: CDN pinned 2.0.4, `defer`, month.html only; no SRI; plain form is the primary path.

## 6. Tests — `tests/test_month.py`
Fixtures: reuse test_app's `client` pattern (env + reload + `seed()`); `today` fixture: `monkeypatch.setattr(appmod, "_today", lambda: date(2026, 9, 14))` on the reloaded module. Helper `_add_change(engine, profile_id, *, url, instrument, effective_dates, decision, reasons, todos, tag="withdrawal")` inserts DocumentRow (markdown containing the quote, regulation_tags), RegChangeRow (one obligation {text, quote}), ApplicabilityRow (todos reuse that quote, citation_url=url).
1. `test_seed_both_groups_in_force` [theater]: "Already in force" once; `in force since <time datetime="2026-06-19">19 June 2026</time>` ×2 and `…30 June 2026` ×2; `class="pill overdue"` count 4; "Coming up", "Due this month", "Nothing due" absent; subline "September 2026 · 4 open · 0 done · 0 unclear".
2. `test_buckets_and_order` [theater]: seed + future todo (2027-01-01) + this-month todo (2026-09-30) + no-date todo + unclear change → headings in order; `due <time datetime="2027-01-01">1 January 2027</time>`; no `pill overdue` after the "Due this month" heading; "no fixed date — see the obligation"; unclear item shows reasons[0], "Update your profile", "Details"; unclear contributes no `.todo`.
3. `test_month_view_pure_sorting`: `_month_view([...], today)` sorted by due asc then instrument; done excluded from n_open.
4. `test_next_hint_from_applies_only` [theater]: fresh profile with only an unclear change → "Nothing due this month." + "Nothing scheduled yet."; add applies change with effective_dates=[2027-03-01], zero todos → "Next: <label>, <time datetime="2027-03-01">1 March 2027</time>."; add nearer open todo 2026-12-01 → Next becomes 1 December 2026.
5. `test_empty_profile_copy`: zero rows → "Nothing due this month." + "New changes appear here after the next crawl."; no `<h2>`.
6. `test_toggle_plain_post_303`: 303 `location: /month`; follow → `class="todo done"` and "Done (1)".
7. `test_toggle_hx_returns_fragment` [theater]: `HX-Request: true` → 200; `r.text.lstrip().startswith('<section id="month-list"')`; no `<html`; `Done (1)`; subline "3 open · 1 done".
8. `test_toggle_persists_and_reverts`.
9. `test_htmx_script_and_attrs` [theater]: `htmx.org@2.0.4`, `hx-post="/todo/`, `hx-target="#month-list"` on /month; `/feed` has no `htmx.org`.
10. `test_all_done_shows_empty_state`: toggle all 4 → "Nothing due this month." + "Nothing scheduled yet." + "Done (4)".
11. (R3) `test_home_redirects_to_month_with_profile`.

## 8. Risks
- `_today()` looked up at call time, never as a default arg. Patch on the reloaded module.
- `hx-swap="outerHTML"` needs the fragment root to be the section.
- Key collision: `_todo_key = sha1(url|quote)[:16]` — two to-dos citing the same quote share a key; seed has none. Log as an issue; do not change the key.
- No red on future items.

## 9. Commits
1. `feat(oct22): month view model (buckets, next hint, unclear), month.html + partials/month_list.html with spelled-out dates; test_app overdue assertion follows ux-brief §3.3`
2. `feat(oct22): toggle returns #month-list fragment on HX-Request (303 otherwise); htmx 2.0.4 with plain-form fallback; / lands on /month; check-button and bucket CSS`
3. `feat(oct22): drop _feed_items (month now on feed_rows); feed subline category names + single-item group subline; issues: todo-key collision, en-only dates`
