<!-- fmt: off -->
# Oct 20 plan — `understand/classify.py` (Nemotron 3 Ultra) → grounded `Applicability`

Orchestrator reconciliation notes R1–R7 (override the plan where they conflict):
- R1: `strict_schema` already exists in `llm_backend.py` (Oct 19, R3) — use it; no move needed. `normalize.response_json_schema()` already delegates.
- R2: Fixture loading: `make_llm_backend("fixtures", settings, dir)` currently opens `<dir>/normalize`. Add a keyword `stage: Literal["normalize","classify"] = "normalize"` to `make_llm_backend` (additive) and open `<dir>/<stage>`; `crawl()` builds two FixtureLLMBackends when `--llm-backend fixtures` (one per stage) but ONE shared LiveLLMBackend when live. `FixtureLLMBackend` keys by `normalize_url` of the `Source URL:` line — your `render_change()` must end with `Source URL: <document_url>` and the retry message too.
- R3: `crawl()` already returns `CrawlRunRow` and `main()` prints lines via `summary_lines(run, out)`; extend `summary_lines` with the classify line and a final `crawl: <n_docs_new> new documents · <n_changes_new> new changes · <n_classified> classified · <tavily_credits> Tavily credits · $<model_cost_usd> model` line; `--stage all` no longer raises; `main()` returns 0. Update Oct 19's `test_stage_all_raises_not_implemented` → rename to `test_stage_all_runs_end_to_end` with the new assertions (that test file `tests/test_crawl_ingest.py` is therefore allowed for this minimal change).
- R4: `run_classify` uses `current_profile(s)`; when None → WARNING and empty summary (exit 0). `seed()` is NOT changed.
- R5: The manifest doc `shopify-help-eu-withdrawal` carries tags ['eudr','withdrawal'] only for tag-merge testing; its classify fixture must be `unclear` (as the plan says) — never `applies` for EUDR.
- R6: Prices are PLACEHOLDERS (`.env.example` comment + FEEDBACK). Oct 20 FEEDBACK entry: offline numbers only, labelled OFFLINE fixture replay; live pending NEBIUS_API_KEY.
- R7: Only core fixtures need real `reasons`/`todos` text as written in the plan; fillers minimal. Every quote in a classify fixture must be byte-identical (whitespace-insensitive) to a quote in that doc's LAST normalize fixture response — the plan's `test_classify_fixture_quotes_match_normalize_fixtures` guards it; build the classify fixtures by reading the normalize fixtures programmatically if that's easier, then commit the JSON.

## 0. Decisions (a–g)
- **a.** `classify()`/`classify_all()` as specified; `classify_all` iterates `regchanges_needing_classify(s, profile_id)` (RegChangeRows with no ApplicabilityRow for that profile) → idempotent by construction. **Profile change:** `POST /profile` inserts a new `ProfileRow`; the next `crawl --stage classify` classifies every RegChange again for the new id; old rows stay (feed filters by `profile_id`). App does not trigger classification itself.
- **b.** Model payload `ClassifiedPayload{decision, reasons, todos[{text, due, quote}]}`; `citation_url := change.document_url` in code. Prompt shows category as `Furniture and wooden goods (furniture_wood)`, ship-to as `Austria (AT), …` or `all 27 EU countries`, establishment as `Germany (DE)` → add `COUNTRY_NAMES: dict[str,str]` (27) + `country_name(code)` to config.
- **c.** Prompt = system message from `prompts/classify.md` + schema appended by code; profile/change go in the **user** message ending with `Source URL: <url>`. Retry = append assistant text + a second user message (addendum, also ending with the `Source URL:` line).
- **d.** Tests build each core `RegChange` by running Oct 19's `normalize()` with `FixtureLLMBackend(tests/fixtures/llm/normalize)` on the extract fixture; profile from `examples/seed-run/profile.json`.
- **e.** `--stage classify` and `--stage all` complete end-to-end; `--stage all --backend fixtures --llm-backend fixtures` runs offline; `test_crawl_all_twice_is_idempotent` written now.
- **f.** Footer model-cost display → Oct 21.
- **g.** Nothing regulation-specific in classify.py.

## 1. Files
**Touch:** `regwatch/understand/classify.py` (rewrite), `regwatch/prompts/classify.md` (new), `regwatch/understand/llm_backend.py` (+`ModelLedger.for_classifier`, +`stage` kwarg on make_llm_backend per R2), `regwatch/config.py` (+`classifier_price_in_per_mtok`, `classifier_price_out_per_mtok`, `COUNTRY_NAMES`, `country_name`), `regwatch/db.py` (+helpers, +CrawlRunRow columns), `regwatch/jobs/crawl.py` (+classify stage), `regwatch/cli.py` (stage choices), `.env.example` (2 price vars), `tests/fixtures/llm/classify/*.json` (16), `tests/test_classify.py`, `tests/test_crawl_all.py`, `tests/test_classify_live.py`, `docs/plans/oct20.md`, `docs/issues.md`, `FEEDBACK.md`, `README.md` (status line).

## 2. Public signatures
```python
# config.py (additive)
classifier_price_in_per_mtok: float = Field(default=1.00, alias="REGWATCH_CLASSIFIER_PRICE_IN_PER_MTOK")   # PLACEHOLDER
classifier_price_out_per_mtok: float = Field(default=3.00, alias="REGWATCH_CLASSIFIER_PRICE_OUT_PER_MTOK") # PLACEHOLDER
COUNTRY_NAMES: dict[str, str] = {"AT": "Austria", "BE": "Belgium", ...}   # all 27, keys == EU_COUNTRIES
def country_name(code: str) -> str   # COUNTRY_NAMES.get(code, code)

# llm_backend.py (additive)
class ModelLedger:  @classmethod def for_classifier(cls, settings: Settings) -> ModelLedger
def make_llm_backend(kind, settings, fixtures_dir=None, *, stage: Literal["normalize","classify"]="normalize") -> LLMBackend

# understand/classify.py
class TodoDraft(BaseModel): model_config = ConfigDict(extra="forbid"); text: str; due: str | None = None; quote: str
class ClassifiedPayload(BaseModel):            # model-facing; citation_url deliberately absent
    model_config = ConfigDict(extra="forbid"); decision: Decision; reasons: list[str]; todos: list[TodoDraft]
@dataclass class ClassifyResult:
    applicability: Applicability | None; retried: bool; dropped_todos: list[Todo]; nulled_dues: int
    usage: LLMUsage | None; cost_usd: float; model: str; error: str | None
@dataclass class ClassifySummary:
    n_changes: int; n_applies: int; n_does_not: int; n_unclear: int; n_retried: int; n_failed: int
    n_todos: int; n_todos_dropped: int; cost_usd: float
    def line(self) -> str   # "classify: 16 changes · 3 apply · 8 don't · 5 unclear · 1 retried · 0 failed · 8 to-dos (1 dropped) · $0.0412"
MAX_TODOS = 6
def load_prompt() -> str                                                    # importlib.resources, prompts/classify.md + "\n## JSON schema\n" + json
def response_format() -> dict[str, Any]                                     # {"type":"json_schema","json_schema":{"name":"classified_payload","strict":True,"schema":strict_schema(ClassifiedPayload)}}
def render_profile(profile: Profile) -> str
def render_change(change: RegChange) -> str                                 # ends with "Source URL: <url>"
def build_messages(change: RegChange, profile: Profile) -> list[dict[str, str]]
def retry_message(change: RegChange, rejected: list[TodoDraft]) -> str      # addendum, ends with Source URL line
def parse_payload(text: str) -> ClassifiedPayload                          # reuse normalize's stripping, then model_validate_json
def enforce(payload: ClassifiedPayload, change: RegChange) -> tuple[Applicability, list[Todo], int]  # (grounded appl, dropped, n_nulled)
def classify(change: RegChange, profile: Profile, *, backend: LLMBackend, settings: Settings, ledger: ModelLedger) -> ClassifyResult
def classify_all(s: Session, *, profile_row: ProfileRow, backend: LLMBackend, settings: Settings, ledger: ModelLedger) -> ClassifySummary

# db.py (additive)
def regchanges_needing_classify(s: Session, profile_id: int) -> list[RegChangeRow]   # ordered by id; not in ApplicabilityRow for profile_id
def store_applicability(s: Session, *, profile_id: int, regchange_id: int, appl: Applicability, model: str, cost_usd: float) -> ApplicabilityRow  # tavily_credits=0; commit
class CrawlRunRow: + n_classified: int = 0; n_applies: int = 0; n_does_not: int = 0; n_unclear: int = 0; n_classify_retried: int = 0; n_classify_failed: int = 0; n_todos: int = 0; n_todos_dropped: int = 0

# jobs/crawl.py — stage order ingest → normalize → classify; each stage additive on the same CrawlRunRow
def run_classify(settings: Settings, s: Session, *, llm_backend: LLMBackend) -> ClassifySummary   # profile = current_profile(s); None → WARNING "no profile yet — open /profile", empty summary
Stage = Literal["all","ingest","normalize","classify"]
#   model_cost_usd = normalize_ledger.cost_usd + classify_ledger.cost_usd; classify counters copied from ClassifySummary
def main(argv=None) -> int   # --stage adds classify; prints one line per stage run + final crawl line (R3)
# cli.py: regwatch crawl --stage {all,ingest,normalize,classify} …
```
**Enforcement order inside `enforce()` (never relaxed):** (1) `citation_url = change.document_url`; (2) `due` parsed as ISO; if not in `change.effective_dates` → `None` + WARNING `classify: nulled invented due=%s url=%s todo=%r`; (3) `does_not` → todos = [] (INFO if any dropped); `unclear` → keep at most the first grounded to-do; (4) truncate to `MAX_TODOS`; (5) `Applicability.ungrounded_todos(change)` → these are the *rejected* list. **`classify()` flow:** call → parse (parse failure counts as rejected-all with `error`) → `enforce` → if rejected non-empty and not yet retried: second call with `retry_message` → `enforce` again → drop what is still rejected (WARNING per to-do); if decision is `applies` and todos == [] → WARNING `applies with 0 grounded to-dos`. Two parse failures → `applicability=None`, `error` set, nothing stored, `n_failed += 1`. Cost = ledger delta across both calls; persisted to `ApplicabilityRow.cost_usd`, `.model = settings.classifier_model`. Log per call at INFO: `classify url=%s model=%s attempt=%d usage=%s cost=$%.4f`.

## 3. `regwatch/prompts/classify.md` (full text; code appends `\n## JSON schema\n<json>`)
```markdown
# RegWatch applicability classifier

You advise the owner of one small online shop in the EU. You receive their store profile and one
regulation change that has already been reduced to numbered obligations, each with a verbatim quote
from the official source. Decide whether this change applies to this shop and, if it does, turn the
obligations into to-dos the owner can act on this month. The owner is not a lawyer.

## Decision — exactly one of
- "applies": the shop's establishment country, ship-to countries or product categories fall inside
  what the obligations cover, and at least one obligation binds this shop as a seller.
- "does_not": the shop is clearly outside the scope for this profile — the text covers product
  categories the shop does not sell, a country it neither sits in nor ships to, or a non-EU rule
  (for example US return rules) that creates no EU obligation.
- "unclear": scope depends on a fact the profile does not contain — annual turnover, headcount,
  share of business customers, where or from what the goods are made, whether the shop counts as an
  operator or a trader. Name that fact. Do not guess it. "unclear" is a good answer, not a failure.
Guidance addressed to one sector (for example coffee importers) does not apply to a shop that sells
something else, even when the underlying regulation would.

## Reasons — 2 to 4 short sentences
- In the owner's own terms, second person, naming the profile facts you used: "applies because you
  ship to France and sell wooden furniture". Never cite article numbers, recitals or legal jargon
  as a reason; the quotes carry the law.
- "unclear": the first sentence starts with "We couldn't tell from your profile:" and says which
  missing fact would decide it and how.
- "does_not": say what the text covers and which profile fact puts the shop outside it.

## To-dos
- Only for "applies". "does_not" → an empty list. "unclear" → an empty list.
- Each to-do turns one numbered obligation into an action. `text` is a verb phrase with an object,
  second person where natural, no legalese: "Add a labelled, two-step withdrawal button to your
  storefront", never "Ensure compliance with the withdrawal function requirement".
- `quote` is that obligation's quote copied character for character from the numbered list — not
  shortened, translated, paraphrased or merged. A to-do whose quote is not one of those exact
  strings is deleted before the owner sees it, so do not write one.
- `due` is one of the change's effective dates (ISO date, copied from the list) or null. Never
  invent a date.
- At most 6 to-dos, at most one per obligation. Skip obligations addressed to someone else
  (authorities, member states, marketplaces) or that ask nothing of this shop.
- Actionable this month by a shop owner: change the storefront, email suppliers, keep a document,
  set up a folder, ask one question.

## Output
Return only one JSON object matching the schema below — no prose, no markdown fences, no reasoning
outside the JSON.
```
**User message (built by code):** `## Store profile\n- Established in: Germany (DE)\n- Ships to: Austria (AT), … | all 27 EU countries\n- Sells: Furniture and wooden goods (furniture_wood); Leather goods (leather)\n- Platform: Shopify\n\n## Regulation change\n- Instrument: …\n- Jurisdiction: …\n- Summary: …\n- Effective dates: 2026-06-19 | none given\n- Obligations:\n  1. <text>\n     quote: "<quote>"\n  …\n\nSource URL: <document_url>`
**Retry addendum (user message 2):** `Your previous answer had {n} to-do(s) whose quote was not copied exactly from the numbered obligations:\n- "{todo.text}" — quote given: "{todo.quote}"\nReturn the complete JSON object again. Keep the decision and reasons unless they were wrong. For each rejected to-do either copy the exact quote of the obligation it belongs to, or leave that to-do out.\n\nSource URL: <document_url>`

## 4. `response_format` notes
`strict_schema(ClassifiedPayload)`: `required` = all props, `additionalProperties: false` on both objects, strip `title`/`default`/`format`; `decision` as `enum`; `due` stays `anyOf [{"type":"string"},{"type":"null"}]`; no `minItems`/`maxItems` (limits enforced in code). Same schema JSON appended to the system prompt.

## 5. Fixtures — `tests/fixtures/llm/classify/<doc-id>.json` (`{"url", "responses":[{"text": <JSON string>, "usage": {...}}]}`, usage ≈ prompt 1900 / completion 420)
Quotes for the two eur-lex docs = seed change-001/002 obligations verbatim; for gii/ihk **copy the quote strings from `tests/fixtures/llm/normalize/<id>.json`** (last response). Core six:
- **eurlex-2023-2673** → `applies`; reasons: "Applies because you sell to consumers in 10 EU countries — every online order from them is a distance contract covered by these rules." / "You are established in Germany, so the same rule reaches you as § 356a BGB, in force since 19 June 2026."; todos: {"Add a labelled, two-step withdrawal button to your storefront", due 2026-06-19, quote = change-001 obligation 1} · {"Label the button 'withdraw contract here' (or an equally clear wording)", 2026-06-19, obligation 2} · {"Send an automatic confirmation email whenever a customer uses it", 2026-06-19, obligation 3}. (If the normalize fixture for this doc uses different quotes than change-001, use the normalize fixture's quotes — R7.)
- **gii-356a-bgb** → `applies`, **two responses** (retry fixture). Response 1: todos[0] with a *shortened* quote (ungrounded) + todos[1] grounded. Response 2: both grounded (quotes from the normalize fixture); reasons: "Applies because your shop is established in Germany, where this paragraph is the binding version of the EU withdrawal-button rule." / "You sell online to consumers in 10 EU countries, so your German storefront must carry the button from 19 June 2026."; to-do texts: "Add a 'Vertrag hier widerrufen' button to your German storefront" / "Confirm every withdrawal to the customer by email straight away", due 2026-06-19.
- **eurlex-2023-1115** → `applies`, one response, todos: {"Ask each furniture and leather supplier for plot geolocation and origin evidence", **due "2025-12-30"** (invented → nulled by code), obligation quote about geolocation} · {"Set up a folder per product line and keep due-diligence documents for five years", 2026-06-30, record-keeping quote}; reasons: "Applies because you sell furniture and wooden goods and leather goods — wood and cattle leather are both covered commodities." / "You place these products on the EU market from Germany, so you count as an operator or trader from 30 June 2026."
- **commission-eudr-coffee-cocoa** → `does_not`, todos []; reasons: "This guidance is written for importers of coffee and cocoa only." / "You sell furniture and wooden goods and leather goods, so nothing on this page asks anything of your shop — your wood and leather products are covered by the EUDR itself, listed separately."
- **shopify-help-us-returns** → `does_not`, todos []; reasons: "This page explains US return and refund rules for orders from US customers." / "You are established in Germany and ship only to 10 EU countries, so these rules do not reach your store." (Note: this doc FAILS normalisation in the Oct 19 fixtures, so it never reaches classify in the pipeline; the unit test builds its RegChange directly with a stub obligation, or the test skips it — see R7 and decide; the 6-label test must still assert 3/2/1 on the six core docs, constructing the RegChange for this one manually from the manifest `why` if normalize yields None.)
- **ihk-eudr-kmu** → `unclear`, todos []; reasons: "We couldn't tell from your profile: the date this applies to you depends on your annual turnover and headcount, which your profile doesn't include." / "If your shop is a micro or small enterprise (under 50 staff and €10 m turnover), the EUDR duties start on 30 June 2026; larger businesses are already covered." / "You do sell wood and leather products, so the regulation itself is relevant either way."
- **10 fillers**, one response each, todos [] — `does_not` (one-sentence reason "This page is <what> and creates no obligation for your shop."): eurlex-2011-83, commission-crd, green-forum-eudr, bmuv-eudr, shopify-blog-eu-withdrawal-button, shopify-blog-eudr; `unclear` ("We couldn't tell from your profile: …"): eurlex-2024-3234, bmj-widerrufsbutton, ihk-widerrufsbutton, shopify-help-eu-withdrawal. (Use the actual manifest ids.)

## 6. Tests (⚑ = load-bearing for the theater check)
`tests/test_classify.py` — helpers: `_profile()` from seed profile.json; `_change(doc_id)` = run `normalize()` with the normalize fixtures over the extract body; `_backend()` = `FixtureLLMBackend(llm_root/"classify")`.
| test | asserts |
|---|---|
| ⚑ `test_core_fixtures_match_expected_labels` (param over manifest `kind=="core"`) | `decision == expected_decision`; aggregate 3/2/1 |
| ⚑ `test_todos_grounded_and_citation_is_ours` | every todo quote ∈ obligation quotes; `citation_url == change.document_url` |
| ⚑ `test_ungrounded_todo_triggers_exactly_one_retry` (gii) | 2 calls, `retried`, `dropped_todos==[]`, retry message contains the shortened quote and ends with `Source URL:` |
| ⚑ `test_invented_due_is_nulled_and_logged` (1115) | first todo `due is None`, second `== date(2026,6,30)`, `nulled_dues==1`, WARNING "nulled invented due" |
| ⚑ `test_applies_with_all_todos_dropped_keeps_decision` (stub backend, same ungrounded JSON twice) | decision applies, todos [], dropped 1, WARNING "0 grounded to-dos" |
| ⚑ `test_does_not_todos_are_stripped_and_unclear_keeps_at_most_one` (stub) | does_not → []; unclear with 2 grounded → 1 |
| `test_reasons_use_profile_terms` | core reasons mention ≥1 of {"Germany","furniture","leather","EU countries"}; unclear `reasons[0].startswith("We couldn't tell from your profile:")`; no reason matches `r"Art\.|Article \d|recital"` |
| `test_prompt_renders_profile_and_change` | user message has "Germany (DE)", "Furniture and wooden goods (furniture_wood)", numbered obligations, ends with `Source URL: `; 27-country profile → "all 27 EU countries"; system message contains `"additionalProperties": false` |
| `test_response_format_is_strict` | strict True; both objects additionalProperties False, required == all keys; decision enum 3 values |
| ⚑ `test_unparseable_twice_is_failed_not_stored` (stub prose) | `applicability is None`, error, 2 calls; `classify_all` n_failed==1, no row |
| ⚑ `test_classify_all_is_idempotent_and_persists_cost` (tmp DB: profile + core changes via `store_regchange`) | run 1: rows == n, each `model == settings.classifier_model`, `cost_usd > 0`; run 2: `n_changes == 0` |
| `test_new_profile_reclassifies_and_keeps_old_rows` | 2nd ProfileRow → more rows; old untouched |
| `test_classify_fixture_quotes_match_normalize_fixtures` | each core classify fixture's LAST response quotes ⊆ that doc's normalized obligation quotes |
`tests/test_crawl_all.py`: ⚑ `test_stage_all_fixtures_offline_end_to_end` (seed profile then `crawl(stage="all", …)` → `stage=="all"`, `n_classified == n_changes_new > 0`, `n_applies == 3`, `model_cost_usd > 0`, `tavily_credits == 36`) · ⚑ `test_crawl_all_twice_is_idempotent` (2nd run: 0/0/0; counts unchanged; 2 CrawlRunRows) · `test_stage_classify_only` · `test_stage_classify_without_profile_is_a_no_op` · `test_main_cli_prints_summary` (`main([... "--stage","all","--backend","fixtures","--llm-backend","fixtures","--no-out"])==0`; stderr matches `classify: \d+ changes · 3 apply`).
`tests/test_classify_live.py` (`@pytest.mark.live`, skip without NEBIUS_API_KEY): classify seed change-001 live → "applies", `ungrounded_todos==[]`, print usage/cost.

## 7. Done when
```
uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q
rm -f regwatch.db && uv run regwatch seed && uv run python -m regwatch.jobs.crawl --stage all --backend fixtures --llm-backend fixtures
uv run python -m regwatch.jobs.crawl --stage all --backend fixtures --llm-backend fixtures
uv run pytest -q tests/test_classify.py -k expected_labels -v
uv run pytest -q -m live tests/test_classify_live.py
```
Expected: gate clean; first `--stage all` prints ingest / normalize / `classify: N changes · 3 apply · D don't · U unclear · 1 retried · 0 failed · T to-dos (0 dropped) · $0.0xxx` / crawl line, rc 0; second run: 0 new documents, 0 new changes, 0 classified; the 6 core label tests pass (3/2/1); live test 1 skipped.

## 8. Risks
- Tokens/cost per call ≈ 1.3–1.9k in, 250–500 out (+ reasoning traces on Ultra); placeholder prices. If traces dominate live, try `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` behind a Settings flag in LiveLLMBackend only — NOT on Oct 20 unless trivial.
- response_format strict/anyOf may be rejected → Oct 19's 400-fallback handles it.
- German quotes: the model may translate/shorten → grounding drops it; retry addendum is explicit.
- Over-eager `applies` on guidance pages: prompt sentence exists; tighten prompt, never code.
- SQLite `create_all` won't add new CrawlRunRow columns → delete local regwatch.db.

## 9. Commits (each green)
1. `feat(oct20): classifier price settings, COUNTRY_NAMES, ModelLedger.for_classifier, make_llm_backend stage, applicability db helpers`
2. `feat(oct20): classify.py — Ultra prompt, strict JSON schema, grounding/due/citation enforced in code, one retry` (+ prompts/classify.md, classify fixtures, tests/test_classify.py)
3. `feat(oct20): crawl --stage classify|all end-to-end offline with CrawlRunRow classify counters; idempotency test`
4. `docs(oct20): live smoke test, FEEDBACK entry, README status`

## 10. FEEDBACK.md — Oct 20 entry (replace the placeholder; OFFLINE numbers, live pending)
```
- (Oct 20) Nemotron 3 Ultra via Token Factory, classify stage. OFFLINE fixture replay: 6/6 labels (3 apply / 2 don't / 1 unclear);
  ungrounded to-dos before retry <a>, after retry <b>; invented dates nulled <c>; tokens/call prompt <p> · completion <q>;
  $<z> per 16-change crawl at PLACEHOLDER $1.00/$3.00 per Mtok. Live accuracy, response_format strict acceptance, reasoning-trace
  size and latency: pending NEBIUS_API_KEY.
```
