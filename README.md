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
  and logged, never shown. Citation URLs are set by code; a due date the model proposes is kept only if it is one of the change's
  effective dates, otherwise it is nulled.
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
`uv run regwatch crawl` (live search + extract + both models; writes nothing to `examples/seed-run`).
The demo seed is recorded from such a crawl with `uv run regwatch record-seed --url … [--exclude …]`
(grounded changes only; undated to-dos skipped; the crawl's Tavily credits carried into the footer).
Schema changes are not migrated — delete a stale local `regwatch.db` first.

## Deploy on Nebius

The public URL serves the recorded run in `examples/seed-run` (recorded with `regwatch record-seed` from the sixth live crawl: 4 documents, 29 changes, 40 Tavily credits); the daily Serverless Job runs the same image with the live crawl entrypoint against a freshly seeded database and is expected to prove in its logs that a second pass adds nothing (verified offline by `tests/test_crawl_all.py::test_crawl_all_twice_is_idempotent`) — live results reach the URL by re-recording the seed and rebuilding the image, not by a shared database, because Serverless Endpoints have no persistent volume we could verify.

One image, two modes (`ops/entrypoint.sh serve|crawl`; `crawl` exits 2 without both keys and never uses
fixtures). Console values: `ops/serverless-endpoint.yaml`, `ops/serverless-job.yaml`; daily schedule:
`.github/workflows/daily-crawl.yml` (Jobs have no cron field); step-by-step: `docs/runbook-nebius.md`.

## Costs and measurement

All figures below are offline fixture replays (Tavily row: the crawl above; model rows: the per-stage
replays recorded in FEEDBACK.md, Oct 19/20 — a full crawl after `seed` costs $0.0345 at the placeholder
prices); **no live call has been made yet — live figures pending** and will replace this block.

| Item | Offline figure | Basis |
|---|---|---|
| Tavily credits per crawl | 36, offline (14 searches, 1 extract of 16 URLs) | documented pricing: 2 per advanced search, 2 per 5 URLs extracted |
| Lightning tokens, 16 documents | 17,330 prompt / 3,900 completion, 18 calls | $0.0058 offline, at PLACEHOLDER $0.20 / $0.60 per Mtok |
| Ultra tokens, 15 changes + 1 retry | 26,200 prompt / 3,490 completion, 16 calls | $0.0367 offline, at PLACEHOLDER $1.00 / $3.00 per Mtok |
| Grounding | quotes verbatim first try 20/22; ungrounded to-dos after retry 0 | `validate_quotes`, `ungrounded_todos` |

Prices live in `Settings` (`.env.example`) and are placeholders until confirmed on the Token Factory
pricing page. Tooling friction with numbers: `FEEDBACK.md`.

## Scope cut

Pre-approved: if live classification of the EU Deforestation Regulation proves unreliable, EUDR is
dropped and the withdrawal function stays; everything else is unchanged.

## Status / what's not done

- Live deploy (Endpoint + Job) pending Moiz's keys and console session — `docs/runbook-nebius.md`.
- Live Tavily and Token Factory numbers pending in this README (see FEEDBACK.md); `examples/seed-run` is recorded from a live crawl (`regwatch record-seed`).
- Open issues: `docs/issues.md`.

## Seed regulations

Directive (EU) 2023/2673 (withdrawal button, § 356a BGB, from 19 June 2026) and Regulation (EU) 2023/1115
(EUDR; SME application from 30 June 2026 — verify, dates have shifted).

## License

MIT
