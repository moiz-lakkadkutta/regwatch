<!-- fmt: off -->
# Oct 19 plan — `understand/normalize.py` (Nemotron 3.5 Lightning) with quote validation + one retry

## 0. Non-negotiables (verbatim)
- Models from env: REGWATCH_EXTRACTOR_MODEL (Nemotron 3.5 Lightning) for normalize; base URL TOKENFACTORY_BASE_URL; key NEBIUS_API_KEY; OpenAI-compatible chat completions. Structured output via `response_format={"type": "json_schema", "json_schema": {"name": ..., "schema": <RegChange-like JSON schema>, "strict": true}}` AND the schema text pasted into the prompt.
- Grounding enforced in code: `RegChange.validate_quotes(markdown)` must return [] before a change is stored. On failure: retry ONCE with the offending quotes listed in the retry message; if still failing, DROP the offending obligations (keep the valid ones if ≥1 remains, else drop the document), log it. Never relax the check in a prompt or code.
- Log model `usage` (prompt/completion tokens) on every call; accumulate cost in USD using per-model prices from Settings (`extractor_price_in_per_mtok`, `extractor_price_out_per_mtok`, env-driven floats, defaults are placeholders) and persist into `CrawlRunRow.model_cost_usd`.
- Fixtures first, network second: `LLMBackend` Protocol, `FixtureLLMBackend` replaying `tests/fixtures/llm/normalize/<doc-id>.json`, `LiveLLMBackend` wrapping `openai.OpenAI(base_url=..., api_key=...)` that refuses an empty key and is imported lazily. Tests offline; live behind `@pytest.mark.live`.
- Copy in the RegChange summary: lead with what to do and by when; legal names second.
- Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q`.
- Prompt lives at `regwatch/prompts/normalize.md`, packaged, loaded via `importlib.resources`.

## 1. Files
**Touch (new):** `regwatch/understand/llm_backend.py` · `regwatch/prompts/__init__.py` (empty) · `regwatch/prompts/normalize.md` · `tests/fixtures/llm/normalize/*.json` (16) · `tests/test_normalize.py` · `tests/test_llm_backend.py` · `docs/plans/oct19.md`.
**Touch (edit, additive only):** `regwatch/understand/normalize.py` (replace stub) · `regwatch/config.py` (Settings fields only) · `regwatch/db.py` (`RegChangeRow.content_hash`, `RegChangeRow.model`, `CrawlRunRow` counters, `documents_needing_normalize`, `store_regchange`) · `regwatch/jobs/crawl.py` (`seed()` sets `content_hash`; `crawl()` stage `normalize`/`all`; `main()` flags) · `regwatch/cli.py` (pass-through flags) · `.env.example` (+3 vars) · `FEEDBACK.md` (Oct 19 entry) · `tests/conftest.py` (+`llm_fixtures_root`, `fixture_llm`).
**Forbidden:** `regwatch/templates/*`, the grounding methods in `schemas.py`, `regwatch/ingest/*`, `understand/classify.py`, `config.SOURCE_DOMAINS`/`SEED_REGULATIONS`, `examples/seed-run/change-*.json`, `pyproject.toml` deps, `uv.lock`, `tests/fixtures/tavily/*`.

## 2. Public signatures
```python
# regwatch/understand/llm_backend.py
class LLMUsage(BaseModel): prompt_tokens: int = 0; completion_tokens: int = 0
class LLMResult(BaseModel): text: str; usage: LLMUsage; model: str
class LLMBackend(Protocol):
    def complete(self, *, model: str, messages: list[dict[str, str]],
                 response_format: dict[str, Any] | None) -> LLMResult: ...
@dataclass
class ModelLedger:                       # one per crawl; the crawl writes it to CrawlRunRow
    price_in_per_mtok: float; price_out_per_mtok: float
    n_calls: int = 0; prompt_tokens: int = 0; completion_tokens: int = 0; cost_usd: float = 0.0
    def add(self, usage: LLMUsage) -> float: ...   # returns this call's cost_usd
    @classmethod
    def for_extractor(cls, settings: Settings) -> ModelLedger: ...
class FixtureMissing(RuntimeError): ...
class FixtureLLMBackend:                 # keyed by the "Source URL: <url>" line of the last user message
    def __init__(self, root: Path) -> None: ...   # loads every *.json; each has "url" + "responses"
    def complete(...) -> LLMResult: ...  # pops responses[url] in order; FixtureMissing when exhausted
    calls: list[list[dict[str, str]]]    # recorded messages, for tests
class LiveLLMBackend:
    def __init__(self, settings: Settings) -> None: ...  # `from openai import OpenAI` inside; ValueError if key == ""
    def complete(...) -> LLMResult: ...  # temperature=0, max_tokens=4096; on 400 mentioning response_format → retry
                                         # once WITHOUT response_format, WARNING once; usage None → zeros + WARNING
def strict_schema(model: type[BaseModel]) -> dict[str, Any]    # (R3) post-processing per §4
def make_llm_backend(kind: Literal["live", "fixtures"], settings: Settings, fixtures_dir: Path | None) -> LLMBackend
```
```python
# regwatch/understand/normalize.py
class ObligationOut(BaseModel):  model_config = ConfigDict(extra="forbid"); text: str; quote: str
class NormalizedPayload(BaseModel):     # what the MODEL returns; document_url is ours, effective_dates are str
    model_config = ConfigDict(extra="forbid")
    instrument: str; jurisdiction: str; summary: str
    effective_dates: list[str] = Field(default_factory=list); obligations: list[ObligationOut]
def response_json_schema() -> dict[str, Any]           # strict_schema(NormalizedPayload)
def load_prompt() -> tuple[str, str]                   # (system prompt with schema pasted, retry addendum template)
def build_messages(document: Document, *, max_chars: int) -> tuple[list[dict[str, str]], bool]  # (messages, truncated)
def parse_payload(text: str) -> NormalizedPayload      # strips <think>…</think>, ``` fences, leading/trailing prose
def to_regchange(payload: NormalizedPayload, document_url: str) -> RegChange  # keeps only YYYY-MM-DD dates, WARN on drops
@dataclass
class NormalizeResult:
    change: RegChange | None; retried: bool; dropped_quotes: list[str]
    usage: LLMUsage; cost_usd: float; truncated: bool; error: str | None   # error ∈ {invalid_json, schema, ungrounded_all_dropped, backend:<ExcName>}
def normalize(document: Document, *, backend: LLMBackend, settings: Settings, ledger: ModelLedger) -> NormalizeResult
@dataclass
class NormalizeSummary:
    n_docs: int; n_ok: int; n_ok_first_try: int; n_retried: int; n_failed: int; n_changes_new: int
    cost_usd: float; prompt_tokens: int; completion_tokens: int
    @property
    def success_rate_no_retry(self) -> float: ...       # n_ok_first_try / n_docs, 0.0 when n_docs == 0
    def line(self) -> str   # "normalize: 16 documents · 14 ok first try (88%) · 1 retried · 1 failed · $0.0123 model"
def normalize_all(s: Session, *, backend: LLMBackend, settings: Settings, ledger: ModelLedger | None = None) -> NormalizeSummary
```
Decisions: (a) `normalize_all` iterates `db.documents_needing_normalize(s)` = DocumentRows with no `RegChangeRow` where `document_url == url AND content_hash == content_hash` — idempotency key is (url, content_hash); `RegChangeRow` gains `content_hash: str = Field(default="", index=True)` and `model: str = ""` (additive). Failures never store anything and never raise; the doc is retried on the next crawl. `seed()` sets `content_hash` from the matching `DocumentRow` so `--stage normalize` after `seed` adds 0 changes for the seed documents. (b) Markdown sent to the model is `markdown[:settings.normalize_max_chars]` (default 60_000); WARNING with the original length when cut; `validate_quotes` always runs on the FULL markdown. (c) `document_url` is set by us; the model schema excludes it. (g) logger `regwatch.understand.normalize`: one INFO per call `model=%s prompt_tokens=%d completion_tokens=%d cost_usd=%.6f retried=%s url=%s`; WARNING `dropped quote (%s): %.80s` per drop; WARNING for truncation and unparsable dates. A quote with < 6 words is treated as offending too (stricter, never looser).

```python
# regwatch/config.py  (Settings; defaults are PLACEHOLDERS — confirm against Token Factory pricing)
extractor_price_in_per_mtok: float = Field(default=0.20, alias="REGWATCH_EXTRACTOR_PRICE_IN_PER_MTOK")
extractor_price_out_per_mtok: float = Field(default=0.60, alias="REGWATCH_EXTRACTOR_PRICE_OUT_PER_MTOK")
normalize_max_chars: int = Field(default=60_000, alias="REGWATCH_NORMALIZE_MAX_CHARS")
# regwatch/db.py
class RegChangeRow: content_hash: str = Field(default="", index=True); model: str = ""     # additive
class CrawlRunRow: n_changes_new: int = 0; n_normalize_retried: int = 0; n_normalize_failed: int = 0  # additive
def documents_needing_normalize(s: Session) -> list[DocumentRow]
def store_regchange(s: Session, change: RegChange, *, content_hash: str, model: str) -> RegChangeRow
# regwatch/jobs/crawl.py
Stage = Literal["all", "ingest", "normalize"]
def crawl(settings, *, stage: Stage = "all", backend=..., llm_backend: LLMBackend | None = None, llm_fixtures_dir: Path | None = None, time_range=..., out=...) -> CrawlRunRow
    # "ingest": as Oct 18. "normalize": no ingest; new CrawlRunRow(stage="normalize"); normalize_all over the DB.
    # "all": ingest → normalize → print both summary lines → raise NotImplementedError("Oct 20 — classify").
    # After normalize: row.model_cost_usd += ledger.cost_usd; row.n_changes_new/… from the summary; finished_at set.
def main(argv: list[str] | None = None) -> int   # + --stage {all,ingest,normalize}, --llm-backend {live,fixtures}, --llm-fixtures-dir
# regwatch/cli.py: `crawl` gains --stage, --llm-backend, --llm-fixtures-dir, prints summary lines to stderr
```

## 3. `regwatch/prompts/normalize.md` (system prompt; `{schema}` replaced at load time; split on the `## Retry addendum` headings)
```markdown
You extract obligations from one official regulatory document for RegWatch, a service that tells small EU online
merchants what they must do and by when. You never invent text: every quote you return is checked by a program
against the source, and any quote that is not found verbatim is discarded together with your work.

## Fields to extract
- instrument: the legal act's official name and number, e.g. "Directive (EU) 2023/2673" or "§ 356a BGB".
  If the page is guidance about an act, name the act, then the page, e.g. "Regulation (EU) 2023/1115 — IHK guidance".
- jurisdiction: "EU", "DE", or "EU; transposed in DE as …". Say which if the text makes it clear; otherwise "unclear".
- summary: two sentences, plain language, for a shop owner. Sentence 1: what a merchant must do and by when.
  Sentence 2: the legal name and who it covers. Lead with the action, not the law.
- effective_dates: every date on which an obligation starts to apply, as ISO "YYYY-MM-DD". Only dates stated in the
  text. If the text gives only a month or year, omit it. Empty list if none.
- obligations: things a business MUST do, MUST NOT do, or MUST ensure. Not background, not definitions, not rights of
  consumers stated without a trader duty, and not advice. Each has:
  - text: one plain-language sentence, ≤ 25 words, starting with a verb ("Provide …", "Keep …", "Do not …").
  - quote: ONE sentence or clause copied EXACTLY from the document, character for character: same language as the
    source (German stays German), same wording, same punctuation, no ellipses, no paraphrase, no added words,
    no "[…]", at least 8 words. Whitespace differences are the only tolerated difference. If you cannot find a
    sentence to copy, do not create the obligation.

## Rules
- Obligations only. Do not write to-dos, recommendations, or applicability judgements; a second step does that.
- Guidance pages (help centres, chambers of commerce) usually restate obligations from the act: quote the page you
  were given, never the act you remember.
- If the document contains no obligations, return an empty obligations list and still fill the other fields.
- Prefer 3–8 obligations for a legal act, fewer for a short guidance page. Quality over count.
- Output ONLY a single JSON object matching the schema below. No markdown fences, no commentary, no reasoning text.

## JSON schema (your reply must validate against it)
{schema}

## Retry addendum
Your previous reply contained quotes that are NOT present verbatim in the document. A program compared them
character by character (ignoring whitespace) and rejected these:
{failed_quotes}
Return the COMPLETE JSON object again. For every rejected quote, either copy the exact sentence from the document
(same language, same punctuation, no ellipses, at least 8 words) or remove that obligation. Keep the obligations
whose quotes were accepted unchanged. Output only the JSON object.

## Retry addendum (invalid JSON)
Your previous reply was not a valid JSON object for the schema: {error}. Output only the JSON object.
```
`{failed_quotes}` renders as one line per quote: `- "<quote>"`. Retry messages = `[system, user, assistant(previous raw reply), user(addendum)]`; the addendum user message must END with the same `Source URL: {url}` line so the fixture backend can key on it. User message = `Source URL: {url}\nSource domain: {source_domain}\nPublished: {published_date or "unknown"}\n\n<document>\n{markdown}\n</document>\n\nSource URL: {url}`.

## 4. `response_format` schema
`strict_schema(Model)` = `Model.model_json_schema()` post-processed: (1) keep `additionalProperties: false` (from `extra="forbid"`); (2) set `required = list(properties)` on the root and on every `$defs` object; (3) delete every `title` and `default` key; (4) no `format` keys (use `list[str]` for dates). Pass `{"type": "json_schema", "json_schema": {"name": "regwatch_normalized_change", "schema": <that>, "strict": True}}`. The same schema, `json.dumps(indent=2)`, replaces `{schema}` in the prompt. Test `test_response_schema_is_strict_compatible`: all objects `additionalProperties is False`, `required == sorted(properties)` (or set-equal), no `title`/`default`/`format` anywhere.

## 5. Fixtures — `tests/fixtures/llm/normalize/<doc-id>.json`, one per manifest entry (16)
Shape: `{"doc_id": "eurlex-2023-2673", "url": "<manifest url>", "responses": [{"text": "<raw model reply>", "usage": {"prompt_tokens": 6100, "completion_tokens": 420}}]}` — a second element is the retry reply. Every quote in a "good" response must be copy-pasted from `tests/fixtures/tavily/extract/<doc-id>.md`.
- `eurlex-2023-2673`, `eurlex-2023-1115`: clean JSON; obligations reuse the seed change-001/002 quotes plus dates `2026-06-19` / `2026-06-30`.
- `gii-356a-bgb`: clean JSON, German quotes, `jurisdiction: "DE"`.
- `commission-eudr-coffee-cocoa`: reply wrapped in ```json fences with a leading "Here is the JSON:" line — proves parse tolerance, counts as first-try OK.
- `ihk-eudr-kmu`: response[0] has 2 obligations, one paraphrased (not in the body); response[1] fixes it → `retried=True`, `dropped_quotes=[]`.
- `shopify-help-us-returns`: response[0] one obligation with an invented quote; response[1] same → dropped, 0 remain → `change=None`, `error="ungrounded_all_dropped"`.
- 10 filler entries: minimal valid JSON, `obligations: []` or one verbatim quote; `usage` small. Totals: 14 first-try OK, 1 retried, 1 failed → 87.5%.

## 6. Tests (all offline; T = fails under the theater check, i.e. with `normalize.py` reverted to the stub)
`tests/test_llm_backend.py`: `test_ledger_cost_uses_settings_prices` (T) · `test_fixture_backend_replays_in_order_then_raises` (T: FixtureMissing on 3rd call) · `test_live_backend_refuses_empty_key` (T: `ValueError`; assert `"openai" not in sys.modules` after importing `llm_backend`) · `test_live_backend_skipped_without_key` (`@pytest.mark.live`; skips when NEBIUS_API_KEY empty).
`tests/test_normalize.py`: `test_prompt_has_schema_and_verbatim_rule` (T) · `test_response_schema_is_strict_compatible` (T) · `test_parse_payload_tolerates_fences_and_prose` (T) · `test_quote_validation_drops_non_verbatim` (T: 1-response fixture with a quote off by one word → `dropped_quotes` has it, change keeps the valid one, WARNING via caplog) · `test_retry_happens_exactly_once` (T: ihk → retried, 2 calls, last user message lists the failed quote; shopify → 2 calls, change None) · `test_document_url_is_ours_not_models` (T: stray `document_url` key → schema error, retry) · `test_cost_accounting_sums_both_calls` (T) · `test_markdown_truncated_but_validated_in_full` (T: `normalize_max_chars=100`, quote past char 100 still accepted, `truncated=True`) · `test_normalize_all_idempotent_by_content_hash` (T: second run 0 calls; bumping one DocumentRow's content_hash re-normalises exactly that one) · `test_normalize_all_rate_at_least_80pct` (T: 16 DocumentRows from manifest bodies → `n_ok_first_try==14`, `n_retried==1`, `n_failed==1`, rate ≥ 0.8, no RegChangeRow for the failed url) · `test_crawl_stage_normalize_updates_run_row_and_prints_line` (T: `main(["--stage","normalize","--llm-backend","fixtures",...])` after ingesting fixtures → stderr contains `normalize: 16 documents · 14 ok first try (88%) · 1 retried · 1 failed · $`, `CrawlRunRow.model_cost_usd > 0`, `n_changes_new==15`) · `test_seed_then_normalize_adds_zero_for_seed_docs` (T; see R6).

## 7. Done when — see the orchestrator's list above.

## 8. Risks
- Token Factory may reject or ignore `response_format`: live backend falls back once without it. Nemotron may emit `<think>` blocks: `parse_payload` strips them.
- Curly quotes, hyphenation, soft hyphens, NBSP: `validate_quotes` normalises whitespace only — do NOT loosen it; failures are dropped and counted.
- German pages: quotes stay German.
- Long EUR-Lex pages: 60k prefix may cut later articles → fewer obligations, never wrong ones; truncation WARNING.
- `create_all` does not add columns to an existing SQLite file: delete `regwatch.db` locally.

## 9. Commits (each green)
1. `feat(oct19): LLMBackend protocol, fixture/live backends, ModelLedger, strict_schema and extractor price settings`
2. `feat(oct19): prompts/normalize.md, NormalizedPayload schema and normalize() with quote validation + one retry` (+ 16 fixtures, unit tests)
3. `feat(oct19): RegChangeRow.content_hash, normalize_all idempotent over DocumentRow, CrawlRunRow counters; seed-run documents trimmed to official texts (R1)`
4. `feat(oct19): crawl --stage normalize/all with summary line, CLI flags, FEEDBACK entry`

## 10. FEEDBACK.md entry (replace the Oct 19 placeholder line; fill offline numbers now, mark live pending)
```
- (Oct 19) Nemotron 3.5 Lightning via Token Factory, normalisation of N documents (M official, K guidance, J German):
  quote-fidelity rate (obligations whose quote was verbatim on first try) = A/B (xx%); documents needing a retry = R;
  documents dropped after retry = D; obligations dropped = Q. Tokens: P prompt / C completion; $X.XXXX at
  $IN/$OUT per Mtok (prices: placeholder). response_format json_schema strict: pending live. Live figures pending NEBIUS_API_KEY.
```

## Orchestrator reconciliation notes (R1–R6; override the plan where they conflict)
- R1 (demo safety — decided by the orchestrator after the Oct 18 verification): the committed `examples/seed-run/documents.jsonl` must contain ONLY documents whose bodies are real legal text at their real URL: the two eur-lex entries (`https://eur-lex.europa.eu/eli/dir/2023/2673/oj`, `https://eur-lex.europa.eu/eli/reg/2023/1115/oj`). Regenerate it to exactly those 2 lines (same line format, sorted by url). In `jobs/crawl.py`, when `--backend fixtures` is used, default `--out` to None (no file written) unless `--out` is given explicitly; `--backend live` keeps the `examples/seed-run/documents.jsonl` default. Update the Oct 18 tests that assumed the fixture run writes to the seed-run default (`test_documents_jsonl_deterministic`, `test_main_stage_ingest_cli`, `test_seed_loads_documents_jsonl_idempotently`) minimally so they pass explicit tmp `--out` paths / use the trimmed file; add `tests/test_seed_run.py::test_seed_run_documents_are_seed_change_sources` asserting every url in seed-run/documents.jsonl equals a `document_url` of a seed change-*.json. `seed()` continues to load documents.jsonl (now 2 docs) and, per the plan, sets `RegChangeRow.content_hash` from the matching DocumentRow.
- R2: `crawl()` now returns `CrawlRunRow` (the plan's decision) — this replaces Oct 18's `IngestSummary` return; update Oct 18's callers/tests minimally (the summaries are still printed to stderr by `main()`/cli). `--stage all` after normalize raises `NotImplementedError("Oct 20 — classify")`; `main()` may catch it, print `crawl: classify lands Oct 20`, and return 2 as Oct 18 did.
- R3: the plan's `FixtureLLMBackend` is keyed by the `Source URL: <url>` line in the last user message. Keep that; Oct 20 relies on it. Loader default root for `--llm-backend fixtures` is `tests/fixtures/llm` with per-stage subfolders (`normalize/`, `classify/`), so `--llm-fixtures-dir` points at the parent. Put `strict_schema(model: type[BaseModel]) -> dict[str, Any]` in `llm_backend.py` (generalised) and have `normalize.response_json_schema()` call it — Oct 20 reuses it.
- R4: Oct 19's normalize fixtures must be produced for all 16 manifest docs as planned; the "≥80%" test runs over DocumentRows built from the 16 manifest bodies (not from seed-run/documents.jsonl).
- R5: Settings price defaults are PLACEHOLDERS; mark them so in `.env.example` comments and in the FEEDBACK entry.
- R6: `test_seed_then_normalize_adds_zero_for_seed_docs` — after `seed()`, `--stage normalize --llm-backend fixtures` must make 0 model calls for the 2 seed docs (their content_hash matches) — this is the guard that the demo never re-normalises the curated seed changes.
