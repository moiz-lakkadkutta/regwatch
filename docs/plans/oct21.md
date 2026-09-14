<!-- fmt: off -->
# Oct 21 plan — Feed + change detail wired to real rows

Orchestrator notes R1–R5 (override the plan where they conflict):
- R1: `config.COUNTRY_NAMES`/`country_name` already exist (Oct 20) — you may use `country_name` in the feed's profile subline ("Established in Germany") if trivial; otherwise leave the subline as-is.
- R2: The footer's model cost: use `CrawlRunRow.model_cost_usd` (run total). The plan's `Footer` needs nothing from classify counters; optionally show `n_classified`/`n_todos` if present — keep it simple.
- R3: `_feed_items` must keep its dict shape for `month()` (Oct 22 replaces it). Reimplement on `feed_rows()` as the plan says; do not delete it.
- R4: The empty-state copy references `regwatch crawl --stage classify` — that stage exists (Oct 20). Keep it.
- R5: `wrong_call_mailto` uses `Settings.report_email` (default "") — add the Settings field per plan and `.env.example` line `REGWATCH_REPORT_EMAIL=`.

## 0. Non-negotiables — CLAUDE.md + UX brief §3. **Oct 21 adds no DB columns**; no htmx today; month.html and `/month`, `/todo/{key}/toggle` untouched (Oct 22).

## 1. Files
**Touch:** `regwatch/db.py` (additive helpers + one dataclass) · `regwatch/config.py` (additive: `REGULATION_LABELS`, `regulation_instrument()`, `Settings.report_email`) · `regwatch/app.py` · `regwatch/templates/base.html` (CSS + footer block only) · `feed.html` · `change_detail.html` · `profile_form.html` (one `<p class="help">`) · `tests/test_pages.py` (new) · `docs/issues.md` · `docs/plans/oct21.md` · `.env.example` (+`REGWATCH_REPORT_EMAIL`).
**Forbidden:** `schemas.py`, `ingest/*`, `understand/*`, `jobs/crawl.py`, `cli.py`, `month.html`, `examples/seed-run/*`, `tests/fixtures/*`, `SOURCE_DOMAINS`/`SEED_REGULATIONS` values, `pyproject.toml` deps.

## 2. Signatures
```python
# regwatch/config.py (additive)
REGULATION_LABELS: dict[str, str] = {"withdrawal": "Withdrawal button for online orders",
                                     "eudr": "Deforestation-free products (EUDR)"}
def regulation_instrument(tag: str) -> str      # SEED_REGULATIONS entry with that tag → ["instrument"]; KeyError if unknown
class Settings: report_email: str = Field(default="", alias="REGWATCH_REPORT_EMAIL")

# regwatch/db.py (additive)
@dataclass(frozen=True)
class FeedRow:                       # one Applicability joined to its change and (outer) source document
    change_id: int; change: RegChange; appl: Applicability
    created_at: datetime             # naive, UTC as stored
    tag: str | None                  # regulation_tag_for(doc)
    source_domain: str               # doc.source_domain, else urlparse(document_url).hostname or ""
    published_date: str | None; fetched_at: datetime | None; content_hash: str
    normalize_model: str; classify_model: str; cost_usd: float; tavily_credits: int
def regulation_tag_for(doc: DocumentRow | None) -> str | None
    # first tag in SEED_REGULATIONS order present in doc.regulation_tags; None if no doc / no known tag
def feed_rows(s: Session, profile_id: int) -> list[FeedRow]
    # select(ApplicabilityRow, RegChangeRow, DocumentRow).join(RegChangeRow, ApplicabilityRow.regchange_id == RegChangeRow.id)
    #   .outerjoin(DocumentRow, DocumentRow.url == RegChangeRow.document_url).where(profile_id == …).order_by(created_at desc)
def feed_row(s: Session, profile_id: int, change_id: int) -> FeedRow | None
def unchecked_changes(s: Session, profile_id: int) -> list[RegChangeRow]   # RegChangeRows with no ApplicabilityRow for this profile, created_at desc
def corpus_counts(s: Session) -> tuple[int, int]   # (n_documents, n_distinct_source_domains)

# regwatch/app.py
@dataclass(frozen=True)
class TodoView: text: str; due: date | None; quote: str; citation_url: str; key: str; done: bool; overdue: bool
@dataclass(frozen=True)
class FeedItem: id: int; change: RegChange; decision: Decision; reasons: list[str]; todos: list[TodoView]
    created_at: datetime; tag: str | None; source_domain: str; next_due: date | None; in_force: bool  # in_force = next_due < today
    published_date: str | None; fetched_at: datetime | None; content_hash: str
    normalize_model: str; classify_model: str; cost_usd: float
@dataclass(frozen=True)
class FeedGroup: key: str; label: str; instrument: str | None; items: list[FeedItem]
    # tagged: key=tag, label=REGULATION_LABELS[tag], instrument=regulation_instrument(tag); untagged: key=change.instrument, label=change.instrument, instrument=None
@dataclass(frozen=True)
class Footer: kind: Literal["seed", "crawl"]; finished_at: datetime | None; n_docs: int; n_sources: int
    tavily_credits: int; model_cost_usd: float; n_allowlist: int  # = len(SOURCE_DOMAINS)
DECISION_RANK: dict[str, int] = {"applies": 0, "unclear": 1, "does_not": 2}
def _item(row: FeedRow, done_keys: set[str], today: date) -> FeedItem
def _group(items: list[FeedItem]) -> list[FeedGroup]   # group order = SEED_REGULATIONS order, then untagged groups by newest item; within group: (DECISION_RANK, -created_at)
def _footer(s: Session, rows: list[FeedRow]) -> Footer  # kind="crawl" iff last_crawl(s) is not None and finished_at set; seed credits = sum(r.tavily_credits)
def fmt_date(d: date | datetime | None) -> str      # "19 Jun 2026" via f"{d.day} {d:%b %Y}"; "" for None
def fmt_stamp(d: datetime | None) -> str            # "21 Oct 2026 14:02 UTC"; stored values are naive UTC — never call astimezone
def wrong_call_mailto(change_id: int, decision: str, to: str = "") -> str
    # f"mailto:{to}?subject=" + quote(f"RegWatch wrong call: change #{change_id} is marked {decision}")
templates.env.filters.update(d=fmt_date, stamp=fmt_stamp)
def _feed_items(profile_id) -> list[dict[str, Any]]    # KEEP shape for month(); reimplement on feed_rows()+_item()
```
Routes: `/feed` → `feed_rows` → `_group` + `unchecked_changes` + `_footer`; `/change/{id}` → `feed_row` or, if None, `s.get(RegChangeRow, id)` → "unchecked" render; else 404 `HTMLResponse("Not found", 404)`. No classification is triggered by any web route.

## 3. Decisions
a. Group by regulation tag via `RegChangeRow.document_url → DocumentRow.regulation_tags` (outer join); heading = plain label, subline = seed instrument; untagged → own group keyed by instrument.
b. Within a group: applies → unclear → does_not, then newest first.
c. Date shown = `created_at`, format "1 Jun 2026".
d. Detail order: summary → why → to-dos → obligations → source metadata → report link.
e. Footer lives in `feed.html`; both states keep the literal "credits used" so `test_feed_shows_both_seed_changes` passes unchanged.
f. applies+0 todos: item meta "No to-dos yet — see the obligations"; does_not: nothing extra; detail for does_not shows no "What to do" section.
g. Changes not yet classified for this profile render in a trailing "Not checked against your profile yet" section (summary only, no pill, no todos); no auto-classification in the web app.
h. No htmx.

## 4. Templates — markup and exact copy
**base.html** — CSS additions (append inside `<style>`): `.group{margin:28px 0 8px}.group h2{margin:0;font-size:1.15rem}.group .law{color:var(--ink3);font-size:13px;margin:2px 0 8px;overflow-wrap:anywhere}` · `.item h3{font-size:1rem;margin:6px 0 4px;overflow-wrap:anywhere}.item h3 a{color:var(--ink);text-decoration:none}` · `.meta{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;color:var(--ink3);font-size:13px}` · `.chip{font:12px ui-monospace,Menlo,monospace;background:var(--no-bg);color:var(--ink2);padding:1px 6px;border-radius:4px;overflow-wrap:anywhere}` · `.todo .due{font-size:14px;color:var(--ink2);margin:2px 0 6px}` · `.source dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;font-size:14px}.source dt{color:var(--ink3)}.source dd{margin:0;overflow-wrap:anywhere}` · `a.wrong{color:var(--ink3)}` · `@media(max-width:420px){h1{font-size:1.3rem}.card{padding:12px 14px}}`.

**feed.html**
```
<h1>Regulation changes for your store</h1>
<p class="muted">Established in {{profile.establishment_country}} · ships to N countries · categories</p>   (unchanged; R1 optional)
{% if not groups and not unchecked %}<div class="card"><p>No changes on file yet.</p><p class="muted">Run <code>regwatch seed</code> for the recorded run, or <code>regwatch crawl</code> to fetch from the official sources.</p></div>{% endif %}
{% for g in groups %}<section class="group" id="{{g.key}}"><h2>{{g.label}}</h2>{% if g.instrument %}<p class="law">{{g.instrument}}</p>{% endif %}
  {% for it in g.items %}<article class="card item">
    <span class="pill {{it.decision}}">PILL</span> <span class="muted">· {{it.created_at|d}}</span>
    <h3><a href="/change/{{it.id}}">{{it.change.instrument}}</a></h3>
    <p>{{it.change.summary}}</p>
    <div class="meta">META <span class="chip">{{it.source_domain}}</span></div>
  </article>{% endfor %}</section>{% endfor %}
{% if unchecked %}<section class="group"><h2>Not checked against your profile yet</h2><p class="law">Your profile changed. The next crawl re-checks every change; to do it now run <code>regwatch crawl --stage classify</code>.</p>
  {% for ch in unchecked %}<article class="card item"><span class="muted">{{ch.created_at|d}}</span><h3><a href="/change/{{ch.id}}">{{ch.instrument}}</a></h3><p>{{ch.summary}}</p></article>{% endfor %}</section>{% endif %}
```
PILL: `applies`→`Applies to you` · `does_not`→`Doesn't apply to you` · `unclear`→`Unclear — one question`.
META by decision: applies with todos → `{{n}} to-do(s) · {% if it.in_force %}in force since {{it.next_due|d}}{% else %}next due {{it.next_due|d}}{% endif %}` (no date when `next_due` None: just count); applies, 0 todos → `No to-dos yet — see the obligations`; unclear → `Answer one question to find out`; does_not → nothing.
Footer (`{% block footer %}`):
- crawl: `Last crawl {{f.finished_at|stamp}} · {{f.n_docs}} documents from {{f.n_sources}} sources · {{f.tavily_credits}} Tavily credits used this crawl · ${{"%.4f"|format(f.model_cost_usd)}} Nemotron on Nebius Token Factory. Sources are limited to an allow-list of {{f.n_allowlist}} official and platform domains.`
- seed: `Showing the recorded seed run · {{f.n_docs}} documents from {{f.n_sources}} source(s) · {{f.tavily_credits}} Tavily credits used when it was recorded · no live crawl yet — run <code>regwatch crawl</code>. Sources are limited to an allow-list of {{f.n_allowlist}} official and platform domains.`
(pluralise inline; `0 documents` if none — never substitute the allow-list count.)

**change_detail.html** (context `item: FeedItem | None`, `change: RegChange`, `change_id`, `mailto`, plus `created_at`, `source_domain`, `published_date`, `fetched_at`, `content_hash`, `normalize_model`)
```
<p><a href="/feed">← All changes</a></p>
{% if item %}<span class="pill {{item.decision}}">PILL</span>{% endif %} <span class="muted">· {{created_at|d}} · <span class="chip">{{source_domain}}</span></span>
<h1>{{change.instrument}}</h1><p>{{change.summary}}</p>
{% if not item %}<div class="card"><h2>Not checked against your profile yet</h2><p>Your profile changed. The next crawl re-checks this change; to do it now run <code>regwatch crawl --stage classify</code>.</p></div>
{% elif item.decision == "unclear" %}<h2>We couldn't tell from your profile</h2><ul>{% for r in item.reasons %}<li>{{r}}</li>{% endfor %}</ul><p><a href="/profile">Answer one question in your store profile</a> and the next crawl decides.</p>
{% else %}<h2>Why this {{"applies to you" if item.decision == "applies" else "doesn't apply to you"}}</h2><ul>reasons</ul>{% endif %}
{% if item and item.decision == "applies" %}<h2>What to do</h2>{% if item.todos %}<div class="card">{% for t in item.todos %}
  <div class="todo{% if t.done %} done{% endif %}"><div><div class="text">{{t.text}}</div>
  <div class="due">{% if t.due %}{% if t.overdue %}<span class="pill overdue">in force since {{t.due|d}}</span>{% else %}by {{t.due|d}}{% endif %}{% else %}no fixed date{% endif %}</div>
  <blockquote>“{{t.quote}}”<cite>— <a href="{{t.citation_url}}" rel="noopener">{{item.source_domain}}</a></cite></blockquote></div></div>{% endfor %}</div>
  {% else %}<p class="muted">No to-dos yet — the obligations below are the source text.</p>{% endif %}{% endif %}
<h2>Obligations in the text</h2>{% for o in change.obligations %}<div class="card"><div>{{o.text}}</div><blockquote>“{{o.quote}}”<cite>— <a href="{{change.document_url}}" rel="noopener">{{source_domain}}</a></cite></blockquote></div>{% endfor %}
<section class="source"><h2>Source</h2><dl><dt>Document</dt><dd><a href="{{change.document_url}}" rel="noopener">{{change.document_url}}</a></dd>
<dt>Jurisdiction</dt><dd>{{change.jurisdiction}}</dd>{% if change.effective_dates %}<dt>Dates</dt><dd>{{change.effective_dates|map("d")|join(" · ")}}</dd>{% endif %}
{% if published_date %}<dt>Published</dt><dd>{{published_date}}</dd>{% endif %}{% if fetched_at %}<dt>Fetched</dt><dd>{{fetched_at|stamp}}</dd>{% endif %}
{% if content_hash %}<dt>Content hash</dt><dd><code>{{content_hash[:12]}}</code></dd>{% endif %}
<dt>Extracted by</dt><dd>{{"recorded seed run" if normalize_model == "seed" else normalize_model}}</dd>
{% if item %}<dt>Classified by</dt><dd>{{item.classify_model}} · ${{"%.4f"|format(item.cost_usd)}}</dd>{% endif %}</dl></section>
<p class="muted"><a class="wrong" href="{{mailto}}">Report a wrong call</a></p>   (mailto only when item; subject "RegWatch wrong call: change #<id> is marked <decision>")
```
**profile_form.html**: after the platform `<select>`: `<p class="help">Platform guidance pages (e.g. Shopify Help) are searched too, so to-dos can point at the setting to change.</p>`.

## 5. Tests — `tests/test_pages.py` (T = fails under theater check)
Fixture `pages_client`: as `client` in test_app (seed()), then insert via SQLModel: DocumentRow+RegChangeRow+ApplicabilityRow for (1) `https://www.ihk.de/eudr-kmu` tag eudr, decision **unclear**, reasons `["We couldn't tell from your profile: do you import the goods yourself or buy from an EU wholesaler?"]`, todos [] · (2) `https://help.shopify.com/en/manual/us-returns` tag withdrawal, **does_not** · (3) `https://www.gesetze-im-internet.de/bgb/__356a.html` tag withdrawal, **applies**, todos [] (obligation only) · (4) a RegChangeRow with **no** ApplicabilityRow (url on bmj.de). Use `markupsafe.escape(q)` when asserting quotes (autoescape).
1. `test_feed_groups_by_regulation_with_label_and_instrument` (T): both `REGULATION_LABELS` values present; `id="withdrawal"` before `id="eudr"`; seed instruments present.
2. `test_feed_orders_applies_before_unclear_before_does_not` (T).
3. `test_feed_pills_all_three_states` (T): "Applies to you", "Doesn't apply to you", "Unclear — one question".
4. `test_feed_item_meta` (T): "2 to-dos · in force since 19 Jun 2026", "No to-dos yet — see the obligations", "Answer one question to find out"; `class="chip">eur-lex.europa.eu`.
5. `test_feed_footer_seed_state` (T): "Showing the recorded seed run", "14 Tavily credits used when it was recorded", "no live crawl yet", "2 documents from 1 source"; not "Last crawl".
6. `test_feed_footer_after_crawl` (T): insert `CrawlRunRow(started_at, finished_at=datetime(2026,10,21,14,2), stage="all", tavily_credits=44, model_cost_usd=0.0312)` → "Last crawl 21 Oct 2026 14:02 UTC", "44 Tavily credits used this crawl", "$0.0312 Nemotron"; not "seed run".
7. `test_feed_unchecked_section_after_profile_change` (T): POST a new profile → feed 200, contains "Not checked against your profile yet" and all instruments; no pill for them.
8. `test_detail_every_todo_links_citation_url` (grounding guard): for change ids 1,2 read todos from DB; each `f'<a href="{t.citation_url}"'` in page and `t.citation_url == change.document_url`; `<blockquote>` count == len(todos)+len(obligations); every blockquote has `<cite>`.
9. `test_detail_unclear_copy` (T): "We couldn't tell from your profile", reasons text, `href="/profile"`, no "What to do".
10. `test_detail_applies_zero_todos_and_does_not` (T).
11. `test_detail_source_metadata_and_mailto` (T): "recorded seed run", "nvidia/Nemotron-3-Ultra-550b-a55b · $0.0600", `content_hash[:12]`, `wrong_call_mailto(1,"applies")` == `mailto:?subject=RegWatch%20wrong%20call%3A%20change%20%231%20is%20marked%20applies` present in page.
12. `test_detail_unchecked_change_renders` (T): `/change/4` 200 with "Not checked against your profile yet"; `/change/999` 404; `/change/1` on empty DB → 303 to `/profile`.
13. `test_helpers`: `fmt_date(date(2026,6,19)) == "19 Jun 2026"`, `regulation_tag_for` prefers SEED order on `["eudr","withdrawal"]`, `_group` on untagged row uses instrument as label (T).
`tests/test_app.py`: unchanged.

## 7. Risks
- Autoescape: `'`→`&#39;`; compare with `markupsafe.escape`; never `|safe`.
- `created_at`/`fetched_at` naive; format as-is + "UTC".
- `overflow-wrap:anywhere` on long strings.
- `seed()` upserts documents.jsonl first, so both seed changes carry tags; if missing, fall back to instrument-keyed groups.
- `_feed_items` reuse by `month()`: keep dict shape.

## 8. Commits
1. `feat(oct21): db.feed_rows/feed_row/unchecked_changes with regulation-tag join, config.REGULATION_LABELS, typed view models and date/mailto helpers` (+ helper tests)
2. `feat(oct21): feed grouped by regulation with decision pills, item meta, unchecked section and seed-vs-crawl footer; platform help text` (+ feed tests; tick issues)
3. `feat(oct21): change detail with why/to-dos/obligations/source metadata, unclear + unchecked states, report-a-wrong-call mailto`
