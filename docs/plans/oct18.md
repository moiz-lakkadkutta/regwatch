<!-- fmt: off -->
# Plan — Oct 18: Tavily search + extract + diff (ingest stage)

Context verified: `tavily-python 0.8.2` (`search`/`extract` accept `include_domains_mode`, `include_usage`; both return plain `dict`, `search` guarantees `results`, `extract` guarantees `results` + `failed_results`). **Gotcha:** `TavilyClient(api_key=None)` silently goes *keyless* and still hits the network — the live backend must refuse an empty key.

## Non-negotiables (verbatim)
- Only config.SOURCE_DOMAINS are searched or extracted. `include_domains_mode="filter"`. Any hit or extracted URL whose host is not in the allow-list (or a subdomain of it) is dropped and logged, never stored.
- Tavily is the only ingestion path (no httpx fetching of pages).
- Log Tavily `usage` on every call (`include_usage=True`; log the response's `usage` dict, and fall back to the plan's credit arithmetic — advanced search 2 credits, advanced extract 2 credits per 5 URLs — if `usage` is absent). Credits per crawl must be persisted so the feed footer can show "credits used this crawl".
- Search params: search_depth="advanced", include_domains=[domains for the regulation tag], include_domains_mode="filter", time_range (default "year"), max_results=10, include_raw_content=False. Extract: extract_depth="advanced", format="markdown", ≤20 URLs per call.
- Fixtures first, network second: tests run offline via a fake backend replaying recorded-shape JSON under tests/fixtures/tavily/. Live calls only under `@pytest.mark.live`. The fake backend must be a small Protocol so search.py/extract.py never import TavilyClient at module import time in tests.
- `regwatch seed` stays idempotent and examples/seed-run stays what the demo shows on first boot.
- Models/keys from env via config.Settings; Settings must not require keys at import.
- Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q`.

## Design decisions (a–h)
- **a. Crawl record:** additive `CrawlRunRow` in db.py (fields as specified). `app.py` `feed()` adds `last_crawl` (latest `CrawlRunRow` by id) and `n_sources` (distinct `DocumentRow.source_domain`, else `len(SOURCE_DOMAINS)`); `credits` = `last_crawl.tavily_credits` if a run exists, else the existing sum of `ApplicabilityRow.tavily_credits`. Footer copy keeps the literal substring "credits used" so **tests/test_app.py is unchanged**. Reason: seed-only boot has no crawl run, so the fallback keeps the demo footer non-zero.
- **b. Document schema:** `Document(url, content_hash, published_date, fetched_at, markdown, source_domain, regulation_tags: list[str])`. `regulation_tags` is needed (a URL can come from both tags; Oct 20 filters by tag). `DocumentRow` gains `regulation_tags: str = "[]"` (JSON) + `to_schema()`.
- **c. CLI:** `python -m regwatch.jobs.crawl --stage ingest|all [--backend live|fixtures] [--fixtures-dir P] [--time-range year] [--out P|--no-out]` via argparse `main(argv)`; `regwatch crawl` in cli.py takes the same options and calls `run_ingest`. `--stage all` runs ingest then raises `NotImplementedError("Oct 19–20")`. Reason: one code path, no duplicated summary logic.
- **d. documents.jsonl:** `--out` default `examples/seed-run/documents.jsonl`; one Document per line, sorted by url, `json.dumps(doc.model_dump(mode="json", exclude={"fetched_at"}), sort_keys=True, ensure_ascii=False)` + `"\n"`. Reason: excluding `fetched_at` is what makes a second run byte-identical; seed sets `fetched_at=now` on load.
- **e. Dedupe:** `normalize_url()` — lowercase scheme+host, drop fragment, drop query params starting with `utm_`, drop trailing slash (keep `/` root), keep `www.`. The normalised URL is the stored key. Reason: catches the real duplicates without breaking source links.
- **f. Hash:** `diff.content_hash` unchanged, applied to `markdown`.
- **g. diff.py:** `upsert_documents(s, docs) -> DiffResult(new, changed, unchanged: list[str])`; changed → overwrite `markdown`, `content_hash`, `fetched_at`, `published_date`, `regulation_tags`; unchanged → touch nothing. Commit once at the end.
- **h. Logging:** stdlib; loggers `regwatch.ingest.search`, `regwatch.ingest.extract`, `regwatch.ingest.diff`, `regwatch.jobs.crawl`. One INFO line per Tavily call: `search tag=%s domain=%s n_results=%d usage=%s credits=%d` / `extract n_urls=%d n_ok=%d n_failed=%d usage=%s credits=%d`; dropped off-list URLs at WARNING. `main()` calls `logging.basicConfig(level=INFO, stream=sys.stderr)`.
- **Seed + documents.jsonl:** do it **on Oct 18**: `seed()` upserts `documents.jsonl` into `DocumentRow` (idempotent by url+hash) so the footer's "<n> sources" and Oct 19's normalize have documents on first boot. `seed()` return value stays "changes added" (test asserts 2 then 0).

## 1. Files
**Touch:** `regwatch/ingest/tavily_backend.py` (new), `regwatch/ingest/search.py`, `regwatch/ingest/extract.py`, `regwatch/ingest/diff.py`, `regwatch/jobs/crawl.py`, `regwatch/cli.py`, `regwatch/db.py` (+`CrawlRunRow`, +`DocumentRow.regulation_tags`/`to_schema`), `regwatch/schemas.py` (+`Document.regulation_tags` only), `regwatch/app.py` (feed context only), `regwatch/templates/feed.html` (footer block only), `tests/fakes.py` (new), `tests/conftest.py` (new), `tests/test_ingest_search.py`, `tests/test_ingest_extract_diff.py`, `tests/test_crawl_ingest.py`, `tests/test_ingest_live.py`, `tests/fixtures/tavily/**` (new), `examples/seed-run/documents.jsonl` (generated), `FEEDBACK.md` (Oct 18 entry), `README.md` (status line only), `pyproject.toml` (add `"tests/fakes.py" = ["E501"]` only if needed).
**Do NOT touch:** `base.html`, `profile_form.html`, `month.html`, `change_detail.html`; `RegChange.validate_quotes` / `Applicability.ungrounded_todos`; `regwatch/understand/*`; `config.py`; `examples/seed-run/change-*.json`, `profile.json`; `tests/test_app.py`, `tests/test_schemas.py`.

## 2. Public signatures
```python
# regwatch/ingest/tavily_backend.py
class TavilyBackend(Protocol):
    def search(self, **kw: Any) -> dict[str, Any]: ...
    def extract(self, **kw: Any) -> dict[str, Any]: ...
@dataclass
class CreditLedger:
    credits: int = 0; n_search_calls: int = 0; n_extract_calls: int = 0
    def add_search(self, resp: dict[str, Any]) -> int      # usage.credits or 2
    def add_extract(self, resp: dict[str, Any], n_urls: int) -> int  # usage.credits or 2*ceil(n/5)
class LiveTavilyBackend:                      # imports TavilyClient inside __init__ only
    def __init__(self, api_key: str) -> None  # raises RuntimeError if api_key == ""
class FixtureTavilyBackend:                   # replays tests/fixtures/tavily
    def __init__(self, root: Path) -> None
def domains_for(tag: str) -> list[str]        # delegate to config.sources_for
def allowed_domain(url: str) -> str | None    # host == d or host.endswith("." + d) → d   (R1)
def is_allowed(url: str) -> bool              # allowed_domain(url) is not None
def normalize_url(url: str) -> str
def make_backend(settings: Settings, kind: Literal["live", "fixtures"], fixtures_dir: Path | None = None) -> TavilyBackend

# regwatch/ingest/search.py
@dataclass(frozen=True)
class Hit: url: str; title: str; published_date: str | None; source_domain: str; regulation_tags: tuple[str, ...]
def search_regulation(backend: TavilyBackend, tag: str, query: str, *, ledger: CreditLedger, time_range: str = "year") -> list[Hit]   # one call per domain in domains_for(tag); off-list hits dropped+logged
def dedupe_hits(hits: Iterable[Hit]) -> list[Hit]   # by normalize_url; merges regulation_tags; sorted by url
def search_all(backend: TavilyBackend, *, ledger: CreditLedger, time_range: str = "year") -> list[Hit]  # SEED_REGULATIONS × domains

# regwatch/ingest/extract.py
EXTRACT_BATCH = 20
def batched(urls: Sequence[str], n: int = EXTRACT_BATCH) -> list[list[str]]
def extract_documents(backend: TavilyBackend, hits: Sequence[Hit], *, ledger: CreditLedger, fetched_at: datetime | None = None) -> list[Document]  # failed_results logged; off-list result URLs dropped; source_domain := allowed_domain(result url)

# regwatch/ingest/diff.py
def content_hash(markdown: str) -> str   # unchanged
@dataclass
class DiffResult: new: list[str]; changed: list[str]; unchanged: list[str]
    @property
    def total(self) -> int
def upsert_documents(s: Session, docs: Sequence[Document]) -> DiffResult

# regwatch/schemas.py (additive)
class Document(BaseModel): ... regulation_tags: list[str] = Field(default_factory=list)

# regwatch/db.py (additive)
class DocumentRow: ... regulation_tags: str = "[]"; def to_schema(self) -> Document  (extend existing)
class CrawlRunRow(SQLModel, table=True): id: int | None; started_at: datetime; finished_at: datetime | None = None; stage: str; tavily_credits: int = 0; model_cost_usd: float = 0.0; n_search_calls: int = 0; n_extract_calls: int = 0; n_docs_new: int = 0; n_docs_changed: int = 0; n_docs_unchanged: int = 0
def last_crawl(s: Session) -> CrawlRunRow | None

# regwatch/jobs/crawl.py
DEFAULT_OUT = SEED_DIR / "documents.jsonl"
@dataclass
class IngestSummary: n_docs: int; new: int; changed: int; unchanged: int; credits: int; n_search_calls: int; n_extract_calls: int; out: Path | None
    def line(self) -> str   # "ingest: 16 documents (16 new, 0 changed, 0 unchanged) · 36 Tavily credits · 14 searches · 1 extract · wrote examples/seed-run/documents.jsonl"
def write_documents_jsonl(docs: Sequence[Document], out: Path) -> None
def read_documents_jsonl(path: Path) -> list[Document]   # fetched_at := now
def run_ingest(settings: Settings | None = None, *, backend: TavilyBackend | None = None, time_range: str = "year", out: Path | None = DEFAULT_OUT) -> IngestSummary   # backend None → make_backend(settings, "live")
def seed(settings: Settings | None = None) -> int   # + upserts documents.jsonl if present
def crawl(settings: Settings | None = None, *, stage: str = "all", backend: TavilyBackend | None = None, time_range: str = "year", out: Path | None = DEFAULT_OUT) -> IngestSummary
def main(argv: Sequence[str] | None = None) -> int   # argparse; prints summary.line() to stderr; `if __name__ == "__main__": raise SystemExit(main())`

# regwatch/cli.py
def crawl(stage: str = "all", backend: str = "live", fixtures_dir: Path | None = None, time_range: str = "year", out: Path = DEFAULT_OUT) -> None

# tests/fakes.py
class RecordingBackend:   # wraps any TavilyBackend; records kwargs
    def __init__(self, inner: TavilyBackend) -> None
    search_calls: list[dict[str, Any]]; extract_calls: list[dict[str, Any]]
    def search(self, **kw: Any) -> dict[str, Any]; def extract(self, **kw: Any) -> dict[str, Any]
# tests/conftest.py fixtures: fixtures_root (Path), fake_backend (RecordingBackend over FixtureTavilyBackend), tmp_settings (Settings with REGWATCH_DB_URL in tmp_path), manifest (parsed manifest.json)
```

## 3. Fixtures — `tests/fixtures/tavily/`
Layout: `manifest.json`, `search/<tag>__<domain>.json` (one per (tag, domain) = 14 files), `extract/<id>.md` (one body per manifest entry).
**manifest.json**: list of `{id, url, source_domain, regulation_tags, kind: "core"|"filler", expected_decision: "applies"|"does_not"|"unclear"|null, why}`. Core six (profile: DE, ships EU-wide, furniture_wood + leather, Shopify):

| id | url | domain | tags | decision | why |
|---|---|---|---|---|---|
| eurlex-2023-2673 | https://eur-lex.europa.eu/eli/dir/2023/2673/oj | eur-lex.europa.eu | withdrawal | applies | distance contracts with EU consumers; must contain the 3 quotes in change-001 verbatim |
| gii-356a-bgb | https://www.gesetze-im-internet.de/bgb/__356a.html | gesetze-im-internet.de | withdrawal | applies | DE-established → § 356a BGB (German text) |
| eurlex-2023-1115 | https://eur-lex.europa.eu/eli/reg/2023/1115/oj | eur-lex.europa.eu | eudr | applies | wood + leather are relevant commodities; must contain the 3 quotes in change-002 verbatim |
| commission-eudr-coffee-cocoa | https://commission.europa.eu/…/eudr-guidance-coffee-cocoa-operators_en | commission.europa.eu | eudr | does_not | guidance only for coffee/cocoa importers — profile sells neither |
| shopify-help-us-returns | https://help.shopify.com/en/manual/…/us-return-refund-rules | help.shopify.com | withdrawal | does_not | US FTC return rules caught by the "withdrawal" query; no EU obligation |
| ihk-eudr-kmu | https://www.ihk.de/…/eudr-kleinstunternehmen-fristen | ihk.de | eudr | unclear | SME/micro deadline depends on turnover/headcount, which the profile does not collect |

(Replace `…` with plausible path segments.) Each core `.md` is a realistic excerpt (≥3 sentences that read as obligations; German for gii/ihk). Ten fillers (short 2–3 line bodies, `expected_decision: null`): eur-lex CELEX:32011L0083 (withdrawal), eur-lex reg 2024/3234 (eudr), commission consumer-rights-directive_en (withdrawal), green-forum deforestation-regulation-implementation_en (eudr), bmj.de widerrufsbutton page (withdrawal), bmuv.de eudr page (eudr), ihk.de widerrufsbutton page (withdrawal), help.shopify.com eu-withdrawal-function (withdrawal), shopify.com/blog eu-withdrawal-button (withdrawal), shopify.com/blog eudr-compliance (eudr) → **16 documents**.
**Search fixture shape** (real keys): `{"query": …, "results": [{"title", "url", "content", "score", "published_date"|null}], "response_time": 0.9, "usage": {"credits": 2}}`. Each (tag, domain) file lists that domain's manifest URLs for the tag; additionally `withdrawal__eur-lex.europa.eu.json` includes the duplicate `https://eur-lex.europa.eu/eli/dir/2023/2673/oj/?utm_source=tavily#art11a`, and `withdrawal__ihk.de.json` includes the off-list `https://www.trustedshops.de/ratgeber/widerrufsbutton/` (simulated filter leak). `FixtureTavilyBackend.search` selects the file by `(kw["include_domains"][0], tag-from-query)`; the backend maps query→tag via `SEED_REGULATIONS`.
**Extract fixture shape:** backend builds `{"results": [{"url", "raw_content": <md>, "images": []}], "failed_results": [{"url", "error": "not in fixtures"}], "response_time": 1.2, "usage": {"credits": 2*ceil(n/5)}}` from manifest + `.md` files; unknown URL → `failed_results`. Expected offline totals: 14 searches (28) + 1 extract of 16 (8) = **36 credits**.

## 4. Tests (all FAIL at fork point: `ImportError`/`NotImplementedError`)
| file | test | asserts |
|---|---|---|
| test_ingest_search.py | test_search_passes_filter_mode_and_params | every recorded search kw has `include_domains_mode=="filter"`, `search_depth=="advanced"`, `max_results==10`, `include_raw_content is False`, `include_usage is True`, `time_range=="year"`, `include_domains==[domain]` |
| ″ | test_search_drops_offlist_hits | trustedshops URL absent from hits; WARNING logged (caplog) |
| ″ | test_dedupe_merges_tags_and_strips_utm | utm/fragment variant collapses to `…/2673/oj`; eur-lex 2673 has tags `("withdrawal",)`; a URL in both tags carries both |
| ″ | test_search_all_covers_every_tag_domain_pair | 14 recorded searches; ledger.credits==28, n_search_calls==14 |
| ″ | test_ledger_falls_back_when_usage_absent | resp without `usage` → +2 (search), +2*ceil(n/5) (extract) |
| ″ | test_live_backend_refuses_empty_key | `LiveTavilyBackend("")` raises RuntimeError; no TavilyClient import needed |
| test_ingest_extract_diff.py | test_extract_batches_at_most_20 | 45 fake hits → 3 extract calls of 20/20/5; kw `extract_depth=="advanced"`, `format=="markdown"`, `include_usage is True` |
| ″ | test_extract_logs_failed_results_and_drops_offlist | unknown URL → not a Document, logged; injected off-list result URL dropped |
| ″ | test_documents_have_hash_domain_tags | 16 Documents; `content_hash==content_hash(markdown)`; `source_domain` is the allow-list key for the host (www.gesetze-im-internet.de → gesetze-im-internet.de) |
| ″ | test_upsert_new_changed_unchanged | first upsert → 16 new; same → 16 unchanged; edited markdown → 1 changed and row's hash/markdown overwritten |
| ″ | test_core_fixtures_contain_seed_quotes | every `obligations[].quote` in change-001/002 is found by `RegChange.validate_quotes` against the matching fixture markdown (guards Oct 19) |
| test_crawl_ingest.py | test_stage_ingest_yields_at_least_15 | `run_ingest(tmp_settings, backend=fake, out=tmp)` → `n_docs>=15`, `new==n_docs` |
| ″ | test_crawl_run_persisted_with_credits | `CrawlRunRow` exists: `tavily_credits==36`, `n_search_calls==14`, `n_extract_calls==1`, stage=="ingest" |
| ″ | test_documents_jsonl_deterministic | run twice → identical bytes; lines sorted by url; no `fetched_at` key |
| ″ | test_second_run_is_idempotent | 2nd run → 0 new, 16 unchanged; 2 CrawlRunRows |
| ″ | test_main_stage_ingest_cli | `main(["--stage","ingest","--backend","fixtures","--out",tmp])==0`; stderr (capsys) matches `^ingest: \d+ documents` |
| ″ | test_stage_all_raises_not_implemented | after ingest, `NotImplementedError` |
| ″ | test_seed_loads_documents_jsonl_idempotently | `seed()==2`, DocumentRow count == lines; `seed()==0`, count unchanged |
| ″ | test_feed_footer_shows_last_crawl | after ingest + seed, `/feed` contains "Last crawl", "sources", "36 Tavily credits used" |

## 5. Live test — `tests/test_ingest_live.py`
`@pytest.mark.live`; `pytest.importorskip("tavily")`; skip if `not Settings().tavily_api_key`. One `search` (withdrawal query, `include_domains=["eur-lex.europa.eu"]`, filter, year, max_results=3) and one `extract` of its first URL through `LiveTavilyBackend`; assert `"usage" in resp` for both and `results` non-empty; assert every result host is allowed; print `credits: search=… extract=…`. Run manually: `uv run pytest -m live -s tests/test_ingest_live.py`.

## 6. Done when
```
uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q
rm -f regwatch.db && uv run python -m regwatch.jobs.crawl --stage ingest --backend fixtures
# stderr last line: ingest: 16 documents (16 new, 0 changed, 0 unchanged) · 36 Tavily credits · 14 searches · 1 extract · wrote examples/seed-run/documents.jsonl
uv run python -m regwatch.jobs.crawl --stage ingest --backend fixtures   # → "16 documents (0 new, 0 changed, 16 unchanged)", git diff on documents.jsonl empty
uv run regwatch seed && uv run regwatch seed                              # seeded 2 / seeded 0
```
Live (when a key exists): `uv run python -m regwatch.jobs.crawl --stage ingest --no-out` → `≥15 documents`; record credits in FEEDBACK.md. Do **not** overwrite `documents.jsonl` from a live run on Oct 18 (the committed file is the fixture run, which Oct 19/20 depend on).

## 7. Risks
- **Keyless SDK:** empty key ≠ error in 0.8.2 → network calls from tests if a `LiveTavilyBackend` is ever built; guard in `__init__` and never construct it in non-live tests.
- **`usage` absent / shape:** treat as `resp.get("usage", {}).get("credits")`; fall back to arithmetic; log both. Real `usage` may also nest per-endpoint — log the whole dict.
- **Extract `failed_results`:** log at WARNING, continue; if all fail, summary still prints and `CrawlRunRow` is written.
- **Rate limits / timeouts:** SDK raises `UsageLimitExceededError`/`TimeoutError`; catch per call, log, continue with remaining domains; never retry in a loop.
- **mypy strict:** `TavilyClient` is untyped → `cast(dict[str, Any], client.search(**kw))`; Protocol `**kw: Any`; JSON columns need explicit `json.loads` typing (`list[str]`).
- **Schema drift:** `DocumentRow` gets a new column; SQLite `create_all` won't migrate an existing `regwatch.db` → delete the (gitignored) local DB; note in README.
- **tests/test_app.py reloads `regwatch.app`:** the module-level `engine` follows `REGWATCH_DB_URL`; new crawl tests must pass `Settings(REGWATCH_DB_URL=...)` explicitly and not depend on the reloaded app module except in the footer test (reuse test_app's fixture pattern).
- **Fixture URLs** for commission/help.shopify/ihk are plausible, not verified; only the three official texts must be real (they are the seed `document_url`s).

## 8. Commits
1. `feat(oct18): Tavily backend protocol, credit ledger and allow-listed search with dedupe` — tavily_backend.py, search.py, search fixtures, test_ingest_search.py, conftest/fakes.
2. `feat(oct18): extract in batches of 20 into Document; hash-diff upsert` — schemas.Document.regulation_tags, DocumentRow.regulation_tags, extract.py, diff.py, manifest + extract fixtures, test_ingest_extract_diff.py.
3. `feat(oct18): crawl --stage ingest with CrawlRunRow, documents.jsonl and feed footer credits` — crawl.py, cli.py, CrawlRunRow, app.py/feed.html footer, seed loads documents, examples/seed-run/documents.jsonl, test_crawl_ingest.py.
4. `feat(oct18): live Tavily smoke test and feedback notes` — test_ingest_live.py, FEEDBACK.md (per R4), README status line.

## 9. Coordination
- **Oct 19 consumes:** `tests/fixtures/tavily/manifest.json` (ids, `expected_decision`, `why`), `tests/fixtures/tavily/extract/<id>.md` (quotes must be verbatim substrings — guarded by `test_core_fixtures_contain_seed_quotes`), `DocumentRow` rows loaded by `seed()`, `CrawlRunRow.model_cost_usd`, `IngestSummary`/`run_ingest` as the first stage of `crawl(stage="all")`.
- **Oct 20 consumes:** `Document.regulation_tags`; the 3/2/1 expected labels in the manifest.
- **Oct 23:** `regwatch crawl --stage ingest` is the Serverless Job entrypoint; `seed()` now also loads documents.
