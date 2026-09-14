<!-- fmt: off -->
# Oct 24 plan — README, FEEDBACK, polish, freeze

Ground truth (offline, fixtures): `seed` → `seeded 2 change(s)`; `crawl --stage all --backend fixtures --llm-backend fixtures --no-out` prints `ingest: 16 documents (14 new, 0 changed, 2 unchanged) · 36 Tavily credits · 14 searches · 1 extract` / `normalize: 14 documents · 12 ok first try (86%) · 1 retried · 1 failed · $0.0045 model` / `classify: 13 changes · 1 apply · 7 don't · 5 unclear · 1 retried · 0 failed · 2 to-dos (0 dropped) · $0.0300` / `crawl: 14 new documents · 13 new changes · 13 classified · 36 Tavily credits · $0.0345 model`. Second run: `0 new, 0 changed, 16 unchanged`, `0 new changes · 0 classified`. Suite: 136 passed, 4 deselected.

## 1. README.md — full replacement text

````markdown
# RegWatch

Tells a small EU online shop **which regulation changes apply to it and what to do by when**, with the
source sentence quoted under every to-do. Nothing is invented: a to-do is stored only if its quote is
verbatim in the official page (`regwatch/schemas.py`, checked in code, never in a prompt).

```bash
uv sync --dev
uv run regwatch seed     # loads the recorded run in examples/seed-run (idempotent)
uv run regwatch serve    # http://localhost:8000  →  /month
```

Demo: `<endpoint-url>` (Nebius Serverless Endpoint — set on deploy; see Status).

## Runs on Nebius · NVIDIA Nemotron · Tavily

| Piece | Role |
|---|---|
| **Nebius Token Factory** — `nvidia/Nemotron-3_5-Lightning` | document → obligations, each with a verbatim quote (`regwatch/understand/normalize.py`) |
| **Nebius Token Factory** — `nvidia/Nemotron-3-Ultra-550b-a55b` | (change, store profile) → applies / doesn't apply / unclear + grounded to-dos (`regwatch/understand/classify.py`) |
| **Nebius Serverless Endpoint** + **Serverless Job** | web app and the daily crawl, one container image (`ops/`) |
| **Tavily** `search` (advanced, `include_domains`, `time_range`) + `extract` (advanced, markdown) | the only ingestion path; one crawl costs **36 Tavily credits** (14 searches × 2 + 1 extract of 16 URLs at 2 per 5 URLs) — measured offline against recorded fixtures; live figures pending |

Only nine domains are ever searched or extracted (`config.SOURCE_DOMAINS`): eur-lex.europa.eu,
commission.europa.eu, green-forum.ec.europa.eu, gesetze-im-internet.de, bmj.de, bmuv.de, ihk.de,
help.shopify.com, shopify.com. The feed footer shows the credits and model cost of every crawl.

## How it works

1. **Search** — `regwatch/ingest/search.py`: Tavily advanced search per (domain × regulation query), domain-filtered, deduped by URL.
2. **Extract** — `regwatch/ingest/extract.py`: Tavily advanced extract in batches of ≤ 20 URLs → `Document` (markdown + content hash).
3. **Diff** — `regwatch/ingest/diff.py`: new / changed / unchanged by content hash; unchanged documents cost no model call.
4. **Normalize** — `regwatch/understand/normalize.py`: Lightning turns a new document into instrument, dates and obligations with verbatim quotes.
5. **Classify** — `regwatch/understand/classify.py`: Ultra decides applies / doesn't / unclear for the store profile and drafts to-dos.
6. **Store** — `regwatch/jobs/crawl.py`: one `CrawlRunRow` per crawl with Tavily credits and model USD; `regwatch crawl --stage all`.

## Why you can trust a to-do

- Quotes are checked in code: `RegChange.validate_quotes()` (quote is a substring of the full page) and
  `Applicability.ungrounded_todos()` (a to-do's quote is one of the obligations) must be empty before
  anything is stored — and `classify_all` re-checks at the store boundary.
- A model answer with an ungrounded quote gets exactly one retry; what is still ungrounded is dropped
  and logged, never shown. Citation URLs and due dates are set by code, not by the model.
- "Unclear" is a first-class answer: the page says which missing profile fact would decide it.
- Every change page has a "Report a wrong call" link (`REGWATCH_REPORT_EMAIL`).

## What the app shows

- `/profile` — four fields (country, ship-to, categories, platform); no account, no email.
- `/feed` — changes grouped by regulation with applies / doesn't / unclear pills; footer with credits and USD.
- `/month` — to-dos bucketed Already in force → Due this month → Coming up → No fixed date; check-off persists (htmx). `/` lands here.
- `/change/<id>` — why it applies in your profile's terms, to-dos with their quote and source link, obligations, source metadata.

## Run it locally

No keys needed (recorded Tavily and model replies from `tests/fixtures/`):

```bash
uv sync --dev
uv run regwatch seed
uv run regwatch crawl --stage all --backend fixtures --llm-backend fixtures --no-out   # prints "… 36 Tavily credits …"
uv run regwatch serve
```

With keys: copy `.env.example` to `.env`, set `TAVILY_API_KEY=` and `NEBIUS_API_KEY=`, then
`uv run regwatch crawl` (live search + extract + both models; writes `examples/seed-run/documents.jsonl`).
Schema changes are not migrated — delete a stale local `regwatch.db` first.

## Deploy on Nebius

The public URL serves the recorded run in `examples/seed-run` (re-recorded from a live crawl before freeze); the daily Serverless Job runs the same image with the live crawl entrypoint against a freshly seeded database and proves in its logs that a second pass adds nothing — live results reach the URL by re-recording the seed and rebuilding the image, not by a shared database, because Serverless Endpoints have no persistent volume we could verify.

One image, two modes (`ops/entrypoint.sh serve|crawl`; `crawl` exits 2 without both keys and never uses
fixtures). Console values: `ops/serverless-endpoint.yaml`, `ops/serverless-job.yaml`; daily schedule:
`.github/workflows/daily-crawl.yml` (Jobs have no cron field); step-by-step: `docs/runbook-nebius.md`.

## Costs and measurement

All figures below are from the offline fixture run above (recorded Tavily and model replies); **no live
call has been made yet — live figures pending** and will replace this block.

| Item | Offline figure | Basis |
|---|---|---|
| Tavily credits per crawl | 36 (14 searches, 1 extract of 16 URLs) | documented pricing: 2 per advanced search, 2 per 5 URLs extracted |
| Lightning tokens, 16 documents | 17,330 prompt / 3,900 completion, 18 calls | $0.0058 at PLACEHOLDER $0.20 / $0.60 per Mtok |
| Ultra tokens, 15 changes + 1 retry | 26,200 prompt / 3,490 completion, 16 calls | $0.0367 at PLACEHOLDER $1.00 / $3.00 per Mtok |
| Grounding | quotes verbatim first try 20/22; ungrounded to-dos after retry 0 | `validate_quotes`, `ungrounded_todos` |

Prices live in `Settings` (`.env.example`) and are placeholders until confirmed on the Token Factory
pricing page. Tooling friction with numbers: `FEEDBACK.md`.

## Scope cut

Pre-approved: if live classification of the EU Deforestation Regulation proves unreliable, EUDR is
dropped and the withdrawal function stays; everything else is unchanged.

## Status / what's not done

- Live deploy (Endpoint + Job) pending Moiz's keys and console session — `docs/runbook-nebius.md`.
- Live Tavily and Token Factory numbers pending; `examples/seed-run` still to be re-recorded from a live crawl.
- Open issues: `docs/issues.md`.

## Seed regulations

Directive (EU) 2023/2673 (withdrawal button, § 356a BGB, from 19 June 2026) and Regulation (EU) 2023/1115
(EUDR; SME application from 30 June 2026 — verify, dates have shifted).

## License

MIT
````

## 2. FEEDBACK.md
Header (replace the first lines): "# Feedback on Nebius, NVIDIA and Tavily tooling — Dated entries with numbers. **Measured:** everything marked OFFLINE (fixture replay of recorded Tavily/model replies, 136 tests). **Pending live keys:** every credit, token, USD, latency and console-step figure; the Oct 23 entry is a template with `__` blanks, filled on the live day." Keep entries Oct 18–23 verbatim. Append:
`- (Oct 24) Freeze, offline. Top frictions so far, with numbers: (1) Tavily `tavily-python 0.8.2` accepts `api_key=None` and still sends HTTP — one guard line in `LiveTavilyBackend.__init__` is what keeps 136 tests network-free; ask: fail fast in the SDK. (2) Nebius Serverless Jobs have no schedule field — replacing one cron line took a 5-step GitHub Actions workflow, 4 repository secrets and 1 variable (`.github/workflows/daily-crawl.yml`). (3) No documented persistent volume for Serverless Endpoints and no confirmed rw filesystem shared with a Job — so the daily Job cannot write the Endpoint's SQLite; the demo URL is refreshed by re-recording the seed and rebuilding (README "Deploy"); default 250Gi disk ≈ $17.75/month unless lowered to 32Gi. (4) Our own packaging bug: `jobs/crawl.py` resolved the seed dir as `Path(__file__).parents[2]/examples/seed-run`, which points into `site-packages` in a non-editable install — fixed Oct 24 with `Settings.seed_dir` (`REGWATCH_SEED_DIR`, Dockerfile sets `/app/examples/seed-run`). No live numbers exist yet.`

## 3. Polish edits (only these)
- **a. mailto wording** — `regwatch/app.py` `wrong_call_mailto`: add `DECISION_WORDS = {"applies": "applies", "does_not": "doesn't apply", "unclear": "unclear"}` and use `DECISION_WORDS.get(decision, decision)` in the subject. Tests: in `tests/test_pages.py` the does_not detail assertion → `"is%20marked%20doesn%27t%20apply"`; in `test_helpers` add `assert wrong_call_mailto(3, "does_not").endswith("is%20marked%20doesn%27t%20apply")`. Tick the issue.
- **b. classify prompt** — `regwatch/prompts/classify.md` To-dos bullet 1: `- Only for "applies". "does_not" → an empty list. "unclear" → an empty list, or at most one to-do that asks the one question that would decide it.` Test `tests/test_classify.py::test_prompt_unclear_rule_matches_enforce`: `assert "at most one to-do that asks the one question" in load_prompt()` (adapt to the return shape). Tick.
- **c.** `tests/test_classify.py`: replace the `# type: ignore[union-attr]` with `assert a is not None` filtering. Tick.
- **d. store-time re-check** — `tests/test_classify.py::test_classify_all_refuses_leaked_ungrounded_todo(tmp_settings, monkeypatch, caplog)`: store one core change via `store_regchange`; `monkeypatch.setattr("regwatch.understand.classify.enforce", lambda payload, change: (Applicability(decision="applies", reasons=["x"], todos=[Todo(text="Do it", quote="NOT IN THE SOURCE", citation_url=change.document_url)]), [], 0))`; stub backend returning a valid applies payload; assert `summary.n_failed == 1`, `summary.n_applies == 0`, no ApplicabilityRow, `"refusing to store ungrounded" in caplog.text` (match the real log text). Tick.
- **e.** `tests/test_seed_run.py`: add `def test_seed_change_files_present(): assert len(CHANGE_FILES) >= 2` (adapt to the module's glob name). Tick.
- **f.** `regwatch/templates/base.html` `.todo button{…}`: width/height 26→32px, add `min-width:32px;flex:none`. No test; check `/month` renders.
- **g. seed dir** — `regwatch/config.py`: `DEFAULT_SEED_DIR = Path(__file__).resolve().parents[1] / "examples" / "seed-run"`; `Settings.seed_dir: Path = Field(default=DEFAULT_SEED_DIR, alias="REGWATCH_SEED_DIR")`. `jobs/crawl.py`: `SEED_DIR = DEFAULT_SEED_DIR` (tests import it), `seed()` reads `settings.seed_dir / …`. Test `tests/test_crawl_all.py::test_seed_honours_seed_dir_setting(tmp_path)`: copytree SEED_DIR → tmp, delete `change-002-eudr.json`, `seed(Settings(REGWATCH_DB_URL=…, REGWATCH_SEED_DIR=str(tmp/"s"))) == 1`. Do NOT add the key to `.env.example`. Dockerfile: keep editable; add `ENV REGWATCH_SEED_DIR=/app/examples/seed-run` + comment. Tick.

## 4. docs/status.md (< 30 lines)
Days merged: Oct 17 `83ce7ed` · 18 `2cec1c3` · 19 `f831474` · 20 `16474d0` · 21 `2266495` · 22 `9a02a38` · 23 `37a80bc` · 24 (this branch). Tests: 136 → ~141 (4 live deselected). Pending: live keys; Endpoint + Job deploy (runbook); re-record `examples/seed-run` from a live crawl; video (Oct 25–26); clean-machine dry run (Oct 27). Asks for Moiz: TAVILY_API_KEY + NEBIUS_API_KEY (local `.env` + SecretStash `regwatch-keys`); confirm the four model prices; approve Endpoint cost above the $5 gate (~$8–15 for Oct 23–28) or pick the smallest preset; one console session for runbook steps 1–10; `REGWATCH_REPORT_EMAIL`. Top 5 open issues (from docs/issues.md): failed-normalize doc re-selected every crawl; `_todo_key` collision; seed re-record from live; unverified nebius CLI/`--args` syntax; seed applicabilities only proven live by `test_classify_live.py`. Freeze checklist output (trimmed) at the bottom.

## 5. Freeze checklist
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q
git status --short
grep -rn "TAVILY_API_KEY=\|NEBIUS_API_KEY=" --include=*.py --include=*.yaml --include=*.md . | grep -v example | grep -v 'API_KEY=$'
git ls-files .env; git check-ignore -v .env
git ls-files examples/
head -40 README.md | grep -c "Token Factory\|Nemotron-3-Ultra\|Nemotron-3_5-Lightning\|Serverless Endpoint\|Serverless Job\|Tavily\|credits"
S=<scratchpad>/fresh; rm -rf $S && time (git clone -q . $S && cd $S && uv sync --dev && uv run regwatch --help && uv run regwatch seed && uv run regwatch crawl --stage all --backend fixtures --llm-backend fixtures --no-out && uv run pytest -q)
```

## 6. Tests and commits
New/changed: ⚑ `test_classify_all_refuses_leaked_ungrounded_todo` · ⚑ `test_seed_honours_seed_dir_setting` · `test_prompt_unclear_rule_matches_enforce` · `test_seed_change_files_present` · edited mailto tests · ⚑ `tests/test_readme.py::test_readme_quotes_the_fixture_crawl_figures`: run the fixture ingest on `tmp_settings` (`crawl(stage="ingest", backend=make_backend(settings,"fixtures"), out=None)` or `run_ingest`), then assert `f"{run.tavily_credits} Tavily credits"` and `f"{run.n_search_calls} searches"` appear in README, both model ids, "Token Factory", "Serverless Endpoint", "Serverless Job" within the first 40 lines, "measured offline" on the same line as "Tavily credits", `"AI-powered" not in text`.
Commits: 1 `feat(oct24): mailto decision wording, classify prompt unclear rule, store-time grounding test, seed glob guard, 32px check button` · 2 `feat(oct24): Settings.seed_dir (REGWATCH_SEED_DIR) used by seed(); Dockerfile ENV` · 3 `docs(oct24): README rewrite — Nebius/Nemotron/Tavily above the fold with offline credit figure; tests/test_readme.py` · 4 `docs(oct24): FEEDBACK consolidation, docs/status.md, issues ticked, freeze checklist output`.

## 7. Risks
- README drift vs code: `test_readme.py` derives credit/search strings from the fixture ingest.
- Stale `> Status:` paragraph: deleted wholesale.
- Hero must not promise a live URL.
- (g) touches `seed()` (both container modes) — covered by the new test + existing seed tests.
