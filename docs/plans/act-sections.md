# Plan — split long legal acts into sections (2026-10-06)

Branch `feat/act-sections`, worktree `../regwatch-act-sections`. No separate spec; authority = `CLAUDE.md`,
`docs/regwatch-plan.md`, `docs/ux-brief.md` §3, and the user's decisions recorded below.

## Why (live evidence, 2026-10-04/06)

Directive (EU) 2023/2673 (`https://eur-lex.europa.eu/eli/dir/2023/2673/oj`, 106k chars) is normalised as ONE change.
Most of its articles concern financial-services contracts, so Lightning frames every obligation as "for financial
services" and Ultra answers `does_not` for a furniture shop (5/5 runs). Art. 11a (withdrawal function, inserted into the
general Consumer Rights Directive 2011/83/EU, applies to ALL online distance contracts) is lost in that framing. A prompt
rule, Lightning thinking-on (32k tokens) and Ultra as extractor all still said `does_not`. Normalising ONLY the Art. 11a
text (amendment point (3) of Article 1, 2,016 chars) → `applies`, 6 grounded to-dos, 2/2 runs.

User decisions: split long acts per article/amendment point, one stored change per section; in the feed, sections that
don't apply are collapsed under the act ("N other provisions … don't apply to you").

## Global Constraints

- Python 3.12, `mypy --strict`, ruff. Done = `uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q` all green (run in the worktree).
- Tests are offline (fixture backends); never call live APIs; `live` marker stays deselected.
- Grounding is untouched and enforced in code: every stored change passes `RegChange.validate_quotes()` against the FULL
  document markdown; every stored applicability passes `Applicability.ungrounded_todos()`. Never relax in a prompt.
- `regwatch seed` stays idempotent; `examples/seed-run` (one change per act today) must still load and display.
- No database schema change: the section label travels in `RegChange.instrument` (`"<act instrument> — <label>"`); the
  act identity is `document_url`.
- Copy (ux-brief §3): lead with what to do and by when; legal names second; "unclear" is first-class.
- Log model `usage` on every call (existing `_Attempt.call` does it); keep the crawl summary lines accurate.
- Match surrounding style: module docstrings, typed helpers, constants/settings in `config.py`.
- Real fixture: `tests/fixtures/acts/dir-2023-2673.md` is the live markdown of Directive (EU) 2023/2673 (106,388 chars).
  Its operative part starts after `HAVE ADOPTED THIS DIRECTIVE:`; `Article 1` has amendment points `(1)Article 3 is
  amended…`, `(2)in Article…`, `(3)the following Article is inserted: ‘Article 11a …`, `(4)the following…`, `(5)in
  Article…`, `(6)in Article…` (no space after the `)`); top-level headings are lines containing only `Article N`.

## Task 1: Section splitter (pure, no model calls)

New module `regwatch/understand/sections.py` (+ `tests/test_sections.py`). Settings field
`act_split_min_chars: int = 20_000` (env `REGWATCH_ACT_SPLIT_MIN_CHARS`) in `config.py`, commented line in `.env.example`.

- `@dataclass(frozen=True) class Section: label: str; text: str`.
- `is_long_act(document: Document, settings: Settings) -> bool`: host is `eur-lex.europa.eu` (or a subdomain), path
  contains `/eli/` or `/legal-content/`, and `len(markdown) > settings.act_split_min_chars`.
- `split_act(markdown: str, *, max_chars: int = 15_000, min_chars: int = 3_000) -> list[Section]`:
  1. Operative body = text after the first line matching `HAVE ADOPTED THIS (DIRECTIVE|REGULATION|DECISION)`
     (case-insensitive). No match → body = whole markdown.
  2. Split the body at top-level headings: a line that is only `Article <n>` (`<n>` = digits + optional letter), label
     `"Article <n>"`; and at lines starting with `ANNEX` (label = that line, ≤ 40 chars). Non-blank text before the first
     heading becomes a piece labelled `"Introductory provisions"`.
  3. An article piece with ≥ 2 amendment points is split at each point: a `(<k>)` that starts a line or follows
     whitespace/`:` and is immediately followed (optional space) by `the following`, `Article`, `Articles`, `in Article`,
     `in Articles`, `Annex`, `point` or `paragraph`. Label `"Article <n>, point (<k>)"`. The article's intro (heading up
     to the first point, e.g. "Amendments to Directive 2011/83/EU … is amended as follows:", capped at 500 chars) is
     prepended to EVERY point piece, separated by a blank line — it tells the model which act is being amended.
  4. Merge runs of adjacent pieces that are each < `min_chars` while the merged text stays ≤ `max_chars`; label
     `"<first label> – <last label>"`.
  5. Split any piece still > `max_chars` at blank-line paragraph boundaries into parts ≤ `max_chars` (a single paragraph
     longer than `max_chars` is cut hard), labels `"<label> (part <i>)"`.
  6. Returns sections in document order; never an empty list for non-blank input.
- Tests (with the real fixture): one section is labelled exactly `"Article 1, point (3)"`, contains `Article 11a`,
  `withdraw from contract here` and the intro `Directive 2011/83/EU is amended as follows`; no section text exceeds
  `max_chars` + 500 (intro); every section's text minus the prepended intro is a verbatim substring of the markdown;
  the concatenated non-intro texts cover the whole operative body (ignoring whitespace). Synthetic tests: no
  `HAVE ADOPTED` → splits the whole text; small articles merge with an en-dash label; an oversize article is split into
  `(part i)` pieces; `is_long_act` true/false table (eur-lex /eli/ long, eur-lex short, help.shopify long).

## Task 2: Normalise long acts per section; seed idempotency per change

Files: `regwatch/understand/normalize.py`, `regwatch/jobs/crawl.py` (`seed`), tests (`tests/test_normalize.py`,
`tests/test_crawl*.py` as appropriate), fixture LLM replies under a test tmp dir or `tests/fixtures/llm/normalize`.
Uses Task 1's `is_long_act`, `split_act`, `Section`.

- Long acts (`is_long_act`) get:
  1. one whole-act call — the existing `normalize(document, …)` path — used ONLY for `instrument`, `jurisdiction` and
     `effective_dates` (its obligations are discarded; nothing from it is stored). If it fails: instrument = the act's
     first non-blank markdown line (≤ 120 chars), jurisdiction `"EU"`, dates `[]`.
  2. one call per `Section`, same system prompt; the user message keeps `Source URL: <url>` as first and last line
     (fixture key) and adds, before `<document>`: `Section: <label> of <act instrument>. Extract obligations from this
     section only; other sections of the act are processed separately. The amended act named in the section text
     decides who an obligation binds.` The `<document>` is the section text. Quotes are validated against the FULL
     document markdown with the existing one-retry logic.
  3. each section result with ≥ 1 grounded obligation becomes a `RegChange` with `instrument = "<act instrument> —
     <label>"`, `jurisdiction` = section's unless it is `"unclear"` (then the act's), `effective_dates` = the section's
     if non-empty else the act's, `document_url` = the act URL. Sections with 0 obligations or that fail are not stored
     (logged). All stored with the act document's `content_hash`.
- Documents that are not long acts follow today's path unchanged.
- `normalize_all`: a long act is one job in the existing pool; its section calls may run sequentially inside that job.
  Per-call ledgers merge as today. `NormalizeSummary`: `n_docs` counts documents; `n_changes_new` counts stored
  changes (sections); a long act with ≥ 1 stored section counts as ok, with none as failed. Keep the summary line format.
- `seed()`: the "already loaded" check becomes per (`document_url`, `instrument`) instead of per `document_url`, so a
  seed file with several sections of one act loads all of them, and re-running adds nothing.
- Tests: with a fixture LLM backend replaying (whole-act reply, then one reply per section in `split_act` order) for a
  small synthetic long act (set `REGWATCH_ACT_SPLIT_MIN_CHARS` low), `normalize_all` stores one change per section with
  the `—` instrument and act dates when the section has none; a section reply with an ungrounded quote is retried then
  dropped; a short eur-lex page and a guidance page still produce exactly one change; seed loads two sections of one URL
  and is idempotent on a second run.

## Task 3: Feed collapses non-applying sections under their act

Files: the feed view model (`regwatch/db.py` `feed_rows`/`FeedRow` and/or `regwatch/app.py`), `regwatch/templates/feed.html`
(+ partials), tests (`tests/test_app.py` or the feed tests). Read `docs/ux-brief.md` §3 first.

- When a `document_url` has more than one change for the profile: keep each `applies` / `unclear` section as its own
  item (title leads with the action as today; the section label appears in the legal-name line via `instrument`); replace
  all of that act's `does_not` sections with ONE line under the act's last shown item, e.g. "7 other provisions of
  Directive (EU) 2023/2673 don't apply to you" (act name = the part of `instrument` before ` — `). If every section is
  `does_not`, show one `does_not` item for the act with that count line instead of N items.
- Single-change documents render exactly as today. Month view and change-detail pages unchanged except that they show the
  sectioned `instrument` text.
- Tests: a profile with an act having 1 applies + 2 does_not sections → feed HTML shows the applies item and the line
  "2 other provisions of <act> don't apply to you", and not the two does_not items; all-does_not act → one item + count;
  single-change documents unchanged (existing tests keep passing).
