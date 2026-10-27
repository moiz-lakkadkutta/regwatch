# RegWatch

Tells a small EU online shop **which regulation changes apply to it and what to do by when**, with the
source sentence quoted under every to-do. Nothing is invented: a to-do is stored only if its quote is
verbatim in the official page (`regwatch/schemas.py`, checked in code, never in a prompt).

```bash
uv sync --dev
uv run regwatch seed     # loads the recorded run in examples/seed-run (idempotent)
uv run regwatch serve    # http://localhost:8000  →  /month
```

Demo: https://port8000-fje4arxh07epk8b.tunnel.applications.eu-west2.nebius.cloud (Nebius Serverless Endpoint `regwatch-web-5`, eu-west2, deployed 2026-10-19 with four example stores, the agent on and five EU laws; serves the recorded seed run; stays up through judging, Dec 1–15).

## Runs on Nebius · NVIDIA Nemotron · Tavily

| Piece | Role |
|---|---|
| **Nebius Token Factory** — `nvidia/Nemotron-3_5-Lightning` | document → obligations, each with a verbatim quote (`regwatch/understand/normalize.py`) |
| **Nebius Token Factory** — `nvidia/Nemotron-3-Ultra-550b-a55b` | (change, store profile) → applies / doesn't apply / unclear + grounded to-dos (`regwatch/understand/classify.py`) |
| **Nebius Serverless Endpoint** + **Serverless Job** | web app and the crawl, one container image (`ops/`) |
| **Tavily** `search` (advanced, `include_domains`, `time_range`) + `extract` (advanced, markdown) | the only ingestion path; one live crawl costs **40 Tavily credits** (14 searches, 2 extracts; 2026-10-06) and 52 in the Nebius Job (2026-10-10, more new pages); 56 Tavily credits measured offline against recorded fixtures (24 searches over five regulations) |

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

- `/profile` — six fields (country, ship-to, categories, platform, company size, where the goods come from; the last two may be "not sure"); no account, no email.
- `/feed` — changes grouped by regulation with applies / doesn't / unclear pills; footer with credits and USD.
- `/month` — to-dos bucketed Already in force → Due this month → Coming up → No fixed date; check-off persists (htmx). `/` lands here.
- `/change/<id>` — why it applies in your profile's terms, to-dos with their quote and source link, obligations, source metadata.

## Run it locally

No keys needed (recorded Tavily and model replies from `tests/fixtures/`):

```bash
uv sync --dev
uv run regwatch seed
uv run regwatch crawl --stage all --backend fixtures --llm-backend fixtures --no-out   # prints "… 56 Tavily credits …"
uv run regwatch serve
```

With keys: copy `.env.example` to `.env`, set `TAVILY_API_KEY=` and `NEBIUS_API_KEY=`, then
`uv run regwatch crawl` (live search + extract + both models; writes nothing to `examples/seed-run`).
The demo seed is recorded from such a crawl with `uv run regwatch record-seed --url … [--exclude …]`
(grounded changes only; undated to-dos skipped; the crawl's Tavily credits carried into the footer).
Schema changes are not migrated — delete a stale local `regwatch.db` first (the example stores'
`slug` / `name` / `blurb` columns are the one exception: `make_engine` adds them to an older DB).

## Example stores

The shared demo profile stays locked (`REGWATCH_PROFILE_LOCKED=true`). The demo URL is open to anyone with
no login, and checking a new store costs about 35 Nemotron 3 Ultra calls (~$0.24), so entering your own store
works only when you run RegWatch yourself. A row of example stores on the feed, month and profile pages
lets a visitor switch between shops whose results were worked out ahead of time (a cookie, nothing
written). Each is `examples/seed-run/stores/<slug>.json`; the default store (`de-furniture`, equal to
`profile.json`) uses the change files' own results, every other store reads
`stores/<slug>.applicability.jsonl`. `uv run regwatch classify-presets` fills that file: Nemotron 3
Ultra on each (store, seed change) pair that has no line yet, the same `enforce()` grounding and
`ungrounded_todos()` check as the crawl, one appended line per result. A rerun only fills gaps, so
after new change files are recorded it classifies just those; `regwatch seed` then loads them.

## Let the agent check (unclear changes)

On a change marked unclear, a button starts an agent: Nemotron 3 Ultra picks one tool per step — search one allow-listed domain (Tavily basic depth), read a page it was shown, ask you a multiple-choice question, or decide — and each step is shown with its Tavily credits and model cost. Its to-dos pass the same `enforce()` grounding as the crawl, an evidence quote must be verbatim in a page it read, and a reason that cites no quote or answer of yours is dropped (an `applies` / `does_not` left without one becomes `unclear`). The result is shown only to the visitor who ran it; the shared seed rows are never written. Caps in code: 6 model steps and 10 Tavily credits per run, $1 of model tokens and 60 Tavily credits per UTC day (counted in the app's database, which resets when the Endpoint restarts) for all visitors together, 5 runs per visitor per hour. `REGWATCH_AGENT_ENABLED=false` (the default) hides it, and agent spend is not counted in the feed footer's credits per crawl.

## Deploy on Nebius

The public URL serves the recorded run in `examples/seed-run` (recorded with `regwatch record-seed` from the sixth live crawl: 4 documents, 40 Tavily credits; re-classified on 2026-10-14 for a small company, 26 changes; plus 7 changes from 5 documents appended on 2026-10-15 with `record-seed --append` from a live crawl of the green-claims, packaging and product-safety acts: 43 Tavily credits; 33 changes in all); the Serverless Job runs the same image with the live crawl entrypoint against a freshly seeded database — Job #1 on 2026-10-10 re-ingested the 4 seed documents as unchanged and left them alone (52 new documents, 47 new changes, 52 Tavily credits, $0.2464 in model tokens; a second pass over one database adding nothing is verified offline by `tests/test_crawl_all.py::test_crawl_all_twice_is_idempotent`) — live results reach the URL by re-recording the seed and rebuilding the image, not by a shared database, because Serverless Endpoints have no persistent volume we could verify.

One image, two modes (`ops/entrypoint.sh serve|crawl`; `crawl` exits 2 without both keys and never uses
fixtures). Console values: `ops/serverless-endpoint.yaml`, `ops/serverless-job.yaml`; step-by-step: `docs/runbook-nebius.md`.
A daily schedule exists as `.github/workflows/daily-crawl.yml` (Jobs have no cron field) but is switched off.

## Costs and measurement

Live figures come from the sixth live crawl (2026-10-06: `seed` + `crawl --stage all` on a fresh database), priced at
the Token Factory catalog prices checked 2026-10-10 (Lightning $0.06 / $0.24, Ultra $1.00 / $3.00 per Mtok). Offline
figures are fixture replays and are labelled as such.

| Item | Figure | Basis |
|---|---|---|
| Tavily credits per crawl | 40 live (14 searches, 2 extracts); 56 offline (24 searches, 1 extract of 16 URLs; the three regulations added 2026-10-15 replay empty searches) | `usage` on every Tavily response; documented pricing for the offline row |
| Lightning (normalise), 29 documents → 60 changes | 682,890 prompt / 58,210 completion tokens, 115 calls | $0.0549 live |
| Ultra (classify), 60 changes | 102,279 prompt / 84,830 completion tokens, 61 calls | $0.3568 live |
| Full crawl | 8 min 03 s | $0.4117 live in model tokens + 40 Tavily credits |
| Grounding | live: 105 to-dos stored, 0 ungrounded; offline: quotes verbatim first try 20/22 | `validate_quotes`, `ungrounded_todos` |

Prices live in `Settings` (`.env.example`). Tooling friction with numbers: `FEEDBACK.md`.

## Scope cut

Pre-approved: if live classification of the EU Deforestation Regulation proves unreliable, EUDR is
dropped and the withdrawal function stays; everything else is unchanged.

## Status / what's not done

- Live since 2026-10-10: the web app on a Nebius Serverless Endpoint (URL above) and the crawl as a Nebius Serverless
  Job, with keys from a Nebius MysteryBox secret; a Job without the secret refuses to run (exit 2, never fixtures).
- No daily schedule (decided 2026-10-19): `.github/workflows/daily-crawl.yml` stays off, because a crawl's results
  reach the URL only by re-recording the seed and redeploying. Crawls run on demand.
- The demo serves the recorded seed run. "Report a wrong call" opens a mail draft to `REGWATCH_REPORT_EMAIL`
  (set on the demo).
- "Let the agent check" ships off by default (`REGWATCH_AGENT_ENABLED`); the public Endpoint has it on, with keys
  and the read-only demo profile (runbook step 12), capped at $1 and 24 Tavily credits per day. From the next
  image there is also an all-time cap over every agent step in the database (`REGWATCH_AGENT_TOTAL_USD`, default
  $10; `REGWATCH_AGENT_TOTAL_CREDITS`, default 300). Once either is used up, the agent switches itself off.
- Not done: dates in English only (no locale), no re-classification right after a profile change (the next crawl
  does it). Open issues: `docs/issues.md`.

## Seed regulations

Directive (EU) 2023/2673 (withdrawal button, § 356a BGB, from 19 June 2026) and Regulation (EU) 2023/1115
(EUDR, as amended by Regulation (EU) 2025/2650: medium and large operators from 30 December 2026, micro and
small operators from 30 June 2027). Added 2026-10-15: Directive (EU) 2024/825 (green claims and guarantee
information, applied from 27 September 2026), Regulation (EU) 2025/40 (PPWR, applies from 12 August 2026; the
e-commerce empty-space rule from 1 January 2030) and Regulation (EU) 2023/988 (GPSR, applies from 13 December 2024).

## License

MIT
