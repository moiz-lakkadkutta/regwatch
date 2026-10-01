# Plan — crawl scope fix (after the first live crawl, 2026-10-01)

Branch `fix/crawl-scope`, worktree `../regwatch-crawl-scope`. No separate spec: the binding authority is
`CLAUDE.md` + `docs/regwatch-plan.md` §1 ("Sources (fixed allow-list) … `shopify.com/blog` + `help.shopify.com`").

## Why

The first live `regwatch crawl --stage all` (2026-10-01 21:33Z, 72 Tavily credits) returned 109 documents:
- 19 Shopify pages outside the intended allow-list: `apps.shopify.com` (16 app-store listings, many as
  `?locale=xx` copies), `cdn.shopify.com` (2 PDFs), `community.shopify.com` (1). Cause: `SOURCE_DOMAINS` has a
  bare `"shopify.com"` key and `allowed_domain()` accepts any subdomain of a key.
- Many language copies of the same page: EU sites as a `_xx` suffix on the last path segment
  (`…/timeshare-directive_es`, `…_it`, `…/implementing-eu-deforestation-regulation-eudr_da`), `?prefLang=fi`,
  eur-lex `/legal-content/sl/TXT…`, help.shopify.com `/fr/manual/…`, `/zh-TW/manual/…`.
- Identical content under several URLs (7 `commission.europa.eu/…/corporate-sustainability…` URLs, same body).
- Normalise ran one document at a time; 5 of 109 were done after 9 minutes.

## Global Constraints

- Python 3.12, `mypy --strict`, ruff. Done = `uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q` all green (run in the worktree).
- Tests are offline (fixtures); never call live APIs; the `live` marker stays deselected.
- Subdomain matching for official hosts must keep working: `www.ihk.de`, `www.wuerzburg.ihk.de`, `hdr4.bmj.de`,
  `www.bmj.de`, `www.bmuv.de`, `www.gesetze-im-internet.de` (and the bare hosts) stay allowed.
- The seed documents must stay allowed and un-deduped: `https://eur-lex.europa.eu/eli/dir/2023/2673/oj`,
  `https://eur-lex.europa.eu/eli/reg/2023/1115/oj`. Existing fixture files and their names stay unchanged.
- Grounding rules (`RegChange.validate_quotes`, `Applicability.ungrounded_todos`) are untouched.
- Every dropped URL is logged at WARNING with the reason (existing style: `log.warning("dropped … url=%s", …)`).
- Match surrounding style: module docstrings, short comments, typed helpers, `config.py` for constants.

## Task 1: Shopify path rule in the allow-list

Files: `regwatch/config.py`, `regwatch/ingest/tavily_backend.py`, tests in `tests/` (extend the existing allow-list tests file; find it with `grep -rn allowed_domain tests`).

- Add to `config.py`, next to `SOURCE_DOMAINS`:
  `SOURCE_URL_PREFIXES: dict[str, tuple[str, ...]] = {"shopify.com": ("www.shopify.com/blog/", "shopify.com/blog/")}`
  with a one-line comment: a key listed here only accepts URLs whose `host + path` starts with one of the prefixes.
- `allowed_domain(url)`: keep the existing exact-or-subdomain host match (keys are tried in `SOURCE_DOMAINS` order, so
  `help.shopify.com` still matches its own key first). When the matched key is in `SOURCE_URL_PREFIXES`, return the key
  only if `host + path` (host lowercased, no scheme, no query) starts with one of its prefixes; otherwise return `None`.
  Update its docstring and the module docstring line that describes it.
- `SOURCE_DOMAINS` keys and `include_domains` passed to Tavily do not change.
- Tests (table-driven), `allowed_domain` →
  - `https://apps.shopify.com/revoka?surface_detail=eu-withdrawal` → None
  - `https://cdn.shopify.com/s/files/1/x.pdf` → None
  - `https://community.shopify.com/t/foo/1` → None
  - `https://www.shopify.com/ph/blog/micro-business` → None
  - `https://www.shopify.com/blog/eu-withdrawal-button` → "shopify.com"
  - `https://help.shopify.com/en/manual/compliance/legal/eu-right-of-withdrawal` → "help.shopify.com"
  - `https://www.ihk.de/pfalz/recht/x`, `https://www.wuerzburg.ihk.de/news`, `https://hdr4.bmj.de/a.html` → their keys
  - `https://eur-lex.europa.eu/eli/dir/2023/2673/oj` → "eur-lex.europa.eu"

## Task 2: Keep English/German copies only, and dedupe identical content

Files: `regwatch/config.py`, `regwatch/ingest/tavily_backend.py` (helper next to `allowed_domain`),
`regwatch/ingest/search.py`, `regwatch/ingest/extract.py`, tests.

- `config.py`: `KEEP_LANGS: frozenset[str] = frozenset({"en", "de"})` and
  `LANG_CODES: frozenset[str]` — the 24 official EU languages as ISO 639-1 codes
  (bg cs da de el en es et fi fr ga hr hu it lt lv mt nl pl pt ro sk sl sv) plus `ru`, `ja`, `ko`, `zh`.
- New `foreign_language(url: str) -> str | None` in `tavily_backend.py`: returns the language code that marks the
  URL as a non-kept language copy, else None. A URL is a foreign copy when any of these markers holds a code in
  `LANG_CODES - KEEP_LANGS` (comparison case-insensitive; for `xx-YY` forms use the part before `-`):
  1. the first path segment is a language code (`/fr/manual/…`, `/zh-TW/manual/…`, `/EN/legal-content/…` is kept);
  2. the path segment right after a `legal-content` segment (`/legal-content/sl/TXT` → "sl");
  3. the last path segment (file extension stripped) ends in `_xx` (`…timeshare-directive_es` → "es",
     `…6d5151ee-…_en` is kept);
  4. a query parameter named `prefLang`, `locale` or `lang` (case-insensitive name).
  Segments that are not in `LANG_CODES` never count (e.g. `/ph/blog`, `…/laenderinformationen/eu/eudr-…`,
  `englisch_wregg`, `0903_EmpCo.html`).
- `search.py` `search_regulation`: after the allow-list check, drop hits where `foreign_language(url)` is not None with
  `log.warning("dropped foreign-language hit tag=%s domain=%s lang=%s url=%s", …)`. Do the same in `extract.py` for
  extract results (defensive, mirrors the allow-list check there).
- `extract.py` `extract_documents`: after building `docs`, collapse documents with the same `content_hash` into one:
  keep the one whose URL is shortest (tie → lexicographically smallest), union and sort `regulation_tags`, keep the
  first non-None `published_date`; log one `log.info("dedupe content_hash=%s kept=%s dropped=%s", …)` per collapse.
  Return order: sorted by url (as now if it already is; otherwise keep current order of the kept docs).
- Tests:
  - `foreign_language` table: the help.shopify `/fr/`, `/zh-TW/`, `/de/`, `/en/` URLs; commission `…_es`, `…_en`,
    `…_en?prefLang=fi`; eur-lex `/legal-content/sl/TXT?uri=…`, `/legal-content/EN/TXT/PDF?uri=…`,
    `/EN/legal-content/summary/…`; green-forum `…eudr_da`; `www.shopify.com/ph/blog/x` (None);
    ihk `…/laenderinformationen/eu/eudr-…` (None); `www.gesetze-im-internet.de/englisch_wregg/englisch_wregg.html` (None);
    `www.bmuv.de/en/residential-wood-heating` (None); both seed eur-lex URLs (None).
  - `search_regulation` with a stub backend returning one foreign and one English result keeps only the English one.
  - `extract_documents` with a stub backend returning two URLs with identical `raw_content` and different tags returns
    one document with the shorter URL and both tags.
  - The existing fixture-based ingest/crawl tests still pass; if a fixture-based count changes because the fixtures
    contain language copies, update the expected number and say so in the report.

## Task 3: Normalise documents concurrently

Files: `regwatch/config.py` (Settings), `regwatch/understand/normalize.py`, `.env.example`, tests.

- `Settings.normalize_concurrency: int = 4` (env `REGWATCH_NORMALIZE_CONCURRENCY`, follow the existing Settings field
  pattern) and a commented line in `.env.example` next to `REGWATCH_NORMALIZE_MAX_CHARS`.
- `normalize_all`: run the per-document `normalize(...)` calls in a `concurrent.futures.ThreadPoolExecutor`
  with `max_workers=settings.normalize_concurrency`; all database work (`store_regchange`, the session) stays on the
  calling thread, and results are consumed in the original `rows` order so stored rows, the summary and logs are
  deterministic. `ModelLedger.add` is not thread-safe: either give it a `threading.Lock`, or pass each call its own
  `ModelLedger` and merge the counters into the shared one on the calling thread — pick one and keep `ledger`'s
  final totals identical to a sequential run. `normalize_concurrency <= 1` must behave exactly like today (no pool
  needed, but a pool of 1 is acceptable).
- A per-document exception from `normalize(...)` must not abort the others: count it as failed, `log.exception` it
  with the url, store nothing for it (same outcome as `result.change is None` today).
- Tests:
  - With the fixture LLM backend, `normalize_all` with concurrency 1 and 4 produce the same stored changes, the same
    `NormalizeSummary` and the same ledger totals.
  - A stub backend that sleeps ~0.2 s per call over 4 documents finishes in well under 4 × 0.2 s with concurrency 4
    (assert < 0.6 s), proving calls overlap.
  - A stub backend that raises for one document: the other documents are stored, `n_failed == 1`.
