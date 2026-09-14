# RegWatch

Tells a small EU online merchant **which regulation changes apply to their store and what to do**,
with the source sentence quoted under every to-do. Ingestion by **Tavily** (domain-filtered search +
extract over official sources only); understanding by **NVIDIA Nemotron 3 Ultra** and **Nemotron 3.5
Lightning** on **Nebius Token Factory**; hosted as a **Nebius Serverless Endpoint** with a daily
**Serverless Job** crawl.

> Status: ingest stage done (Oct 18) — `regwatch crawl --stage ingest` searches + extracts the allow-list via Tavily and diffs by hash; offline: `--backend fixtures`. Normalise/classify land Oct 19–20 (see `docs/regwatch-plan.md`). Schema changed on Oct 18: delete a pre-existing local `regwatch.db` before running.

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
