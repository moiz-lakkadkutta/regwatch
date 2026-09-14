# RegWatch

Tells a small EU online merchant **which regulation changes apply to their store and what to do**,
with the source sentence quoted under every to-do. Ingestion by **Tavily** (domain-filtered search +
extract over official sources only); understanding by **NVIDIA Nemotron 3 Ultra** and **Nemotron 3.5
Lightning** on **Nebius Token Factory**; hosted as a **Nebius Serverless Endpoint** with a daily
**Serverless Job** crawl.

> Status: ingest (Oct 18), normalise (Oct 19) and classify (Oct 20) done — `regwatch crawl --stage all` searches + extracts the allow-list via Tavily, diffs by hash, turns each new document into obligations with verbatim quotes (Nemotron 3.5 Lightning) and decides applies / doesn't / unclear with grounded to-dos for the current profile (Nemotron 3 Ultra; `citation_url` and due dates set in code, ungrounded to-dos dropped after one retry). Offline: `--backend fixtures --llm-backend fixtures`. Schema changed on Oct 20 (CrawlRunRow classify counters): delete a pre-existing local `regwatch.db` before running. Web app (Oct 21): the feed groups changes by regulation with applies / doesn't / unclear pills and a "not checked against your profile yet" section after a profile change; each change page shows why, the to-dos with their verbatim quote and source link, the obligations, source metadata and a "Report a wrong call" link (`REGWATCH_REPORT_EMAIL`); the footer distinguishes the recorded seed run from the last live crawl (Tavily credits + Nemotron USD). Month view is rebuilt on Oct 22 (see `docs/regwatch-plan.md`).

```bash
uv sync --dev
uv run regwatch seed          # loads examples/seed-run (idempotent)
uv run regwatch serve         # http://localhost:8000
```

## Runs on Nebius · uses NVIDIA models · built on Tavily

| Piece | Role |
|---|---|
| Tavily `search` (advanced, `include_domains` filter, `time_range`) + `extract` (advanced, markdown) | the whole ingestion layer; credits per crawl shown in the app footer |
| `nvidia/Nemotron-3_5-Lightning` via Token Factory | document → obligations with verbatim quotes |
| `nvidia/Nemotron-3-Ultra-550b-a55b` via Token Factory | (change, store profile) → applies / doesn't / unclear + grounded to-dos |
| Nebius Serverless Endpoint / Job | web app / scheduled crawl, same container image |

## Why it's trustworthy

Nothing is invented: a to-do is stored only if its quote is one of the obligations, and an obligation
is stored only if its quote appears verbatim in the extracted source. See `regwatch/schemas.py`.

## Seed regulations

Directive (EU) 2023/2673 (withdrawal function, § 356a BGB, from 19 June 2026) and the EU Deforestation
Regulation 2023/1115 (SME application from 30 June 2026 — verify current status; dates have shifted).

## License

MIT
