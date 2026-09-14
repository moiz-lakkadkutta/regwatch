# RegWatch — Build Plan

Repo `regwatch` · Best Apps & Agents track + Best Use of Tavily · MIT · build Oct 17–24, freeze Oct 24, submit Oct 28

Ten build-days. Every step below is sized so that a day that slips costs a feature, not the submission. The plan is written so a Claude Code session can execute a step from this document alone.

## 0. One-paragraph product

RegWatch tells a small EU online merchant which regulation changes apply to *their* store and what to do about them, with the source cited. The merchant fills a two-minute store profile (where they are established, where they ship, what they sell, which platform). A scheduled job searches a fixed list of official sources through Tavily, extracts new or changed documents, and a Nemotron 3 Ultra classifier decides whether each change applies to that profile and drafts a plain-language to-do. The merchant sees a dated feed and a "this month" panel. Nothing is invented: every to-do links to the paragraph it came from.

## 1. Fixed decisions

| Decision | Value |
|---|---|
| Stack | Python 3.12, FastAPI, Jinja2 templates, htmx for the few interactive bits, SQLite for the demo (Postgres on Nebius if time allows — it won't be needed for judging) |
| LLM | Token Factory; `CLASSIFIER=nvidia/Nemotron-3-Ultra-550b-a55b`, `EXTRACTOR=nvidia/Nemotron-3_5-Lightning` |
| Research | Tavily `search` (`search_depth="advanced"`, `include_domains`, `time_range`, `max_results=10`) + `extract` (`extract_depth="advanced"`, `format="markdown"`, ≤20 URLs per call) |
| Sources (fixed allow-list) | `eur-lex.europa.eu`, `commission.europa.eu`, `gesetze-im-internet.de`, `bmj.de`, `bmuv.de` (EUDR), `green-forum.ec.europa.eu`, `shopify.com/blog` + `help.shopify.com` (platform guidance), `ihk.de` |
| Seed regulations | (1) Withdrawal function — Directive (EU) 2023/2673, §356a BGB, applies from 19 June 2026; (2) EUDR — Regulation (EU) 2023/1115, SME/micro enforcement from 30 June 2026 (verify current status at build time; dates have shifted before) |
| Demo profile | German-established store, ships EU-wide, sells furniture (wood) and leather goods, Shopify |
| Deploy | web app as a Nebius Serverless Endpoint (container); daily crawl as a Nebius Serverless Job (same image, different entrypoint) |
| Never | present a to-do without a citation; classify outside the allow-list; store merchant data beyond the profile |

## 2. Repository layout

```
regwatch/
├── README.md · LICENSE (MIT) · FEEDBACK.md · pyproject.toml · .env.example
├── regwatch/
│   ├── app.py            # FastAPI: routes for profile, feed, month view, change detail
│   ├── config.py         # env + source allow-list + seed regulations
│   ├── schemas.py        # Profile, Source, Document, RegChange, Applicability, Todo
│   ├── db.py             # SQLModel/SQLite; tables mirror schemas
│   ├── ingest/
│   │   ├── search.py     # Tavily search per (source, regulation, time_range)
│   │   ├── extract.py    # Tavily extract → Document (url, hash, published, markdown)
│   │   └── diff.py       # new vs changed (content hash) vs unchanged
│   ├── understand/
│   │   ├── normalize.py  # Lightning: Document → RegChange{instrument, jurisdiction, summary, effective_dates, obligations[]}
│   │   └── classify.py   # Ultra: (RegChange, Profile) → Applicability{applies|does_not|unclear, reasons[], todos[{text, citation}]}
│   ├── jobs/crawl.py     # the scheduled job: search → extract → diff → normalize → classify → store
│   └── templates/        # base, profile_form, feed, month, change_detail
├── ops/Dockerfile · ops/serverless-endpoint.yaml · ops/serverless-job.yaml
├── examples/demo-profile.json · examples/seed-run/   # a recorded crawl so the demo works offline
└── tests/                # schema tests; classify tests on 6 fixture documents (3 apply, 2 don't, 1 unclear)
```

## 3. Data model

```
Profile(id, establishment_country, ship_to[], categories[], platform, created_at)
Source(domain, regulation_tags[])
Document(url, content_hash, published_date, fetched_at, markdown, source_domain)
RegChange(id, document_url, instrument, jurisdiction, summary, effective_dates[], obligations[{text, quote, anchor}])
Applicability(profile_id, regchange_id, decision, reasons[], todos[{text, due, citation_url, quote}], model, cost_usd)
```

`obligations[].quote` is the verbatim sentence from the document; `todos[].quote` must be one of those quotes. That is how "nothing is invented" is enforced in code, not in the prompt.

## 4. Day-by-day plan

| Day | Step | Done when |
|---|---|---|
| Oct 17 (Fri) | Repo, `.env`, `config.py` with allow-list + seed regs; `schemas.py`; `db.py`; profile form + save | `POST /profile` stores the demo profile; page renders it back |
| Oct 18 (Sat) | `ingest/search.py`: for each (source domain × regulation query × `time_range="year"`) run Tavily advanced search; dedupe by URL; `ingest/extract.py`: batch ≤20 URLs, store `Document` with content hash | `python -m regwatch.jobs.crawl --stage ingest` yields ≥15 documents across both regulations; `examples/seed-run/documents.jsonl` written |
| Oct 19 (Sun) | `understand/normalize.py` with Lightning + JSON schema; `obligations[].quote` must be a substring of the markdown (validated in code, retry once) | ≥80% of documents normalise without retry; failures logged, not fatal |
| Oct 20 (Mon) | `understand/classify.py` with Ultra; prompt takes the profile + RegChange; each todo's quote validated against obligations | on the 6 fixtures: 3 apply, 2 don't, 1 unclear — matches expected labels |
| Oct 21 (Tue) | Feed page (dated, grouped by instrument, decision pill, cost footer) + change detail page (summary, obligations with quotes, todos with citations, "why this applies to you") | pages render from SQLite; every to-do has a working source link |
| Oct 22 (Wed) | Month view ("what to do this month"): todos with due dates from `effective_dates`, sorted by due; "mark done" via htmx; empty state copy | demo profile shows the withdrawal-function to-dos as overdue-since-June and EUDR to-dos with the SME date |
| Oct 23 (Thu) | Dockerfile; deploy Endpoint; deploy Job on a daily schedule with the same image; `examples/seed-run` used to preload the DB on first boot so the URL is never empty | public URL loads the feed on a phone; Job runs once manually and adds nothing new (idempotent) |
| Oct 24 (Fri) | README (Runs on Nebius + Tavily section above the fold; how Tavily is used: domain filter, date window, extract, credit usage per crawl); FEEDBACK notes; freeze | a stranger can run it locally in 10 minutes |
| Oct 25–26 | Video (2:00, script in runbook §7) | uploaded, < 3:00 |
| Oct 27 | clean-machine dry run | checklist §7 green |

If Oct 19–20 slip: drop the EUDR regulation, keep the withdrawal function only, and keep every other step.

## 5. Prompts

**normalize.md** (Lightning) — "Extract the legal instrument, jurisdiction, a 2-sentence summary, effective dates, and a list of obligations. Each obligation must include a verbatim quote copied exactly from the text. Output only the JSON schema." Schema pasted in.

**classify.md** (Ultra) — "You are advising a small online merchant. Given their profile and one regulation change, decide whether it applies (applies / does_not / unclear). Give reasons that reference the profile fields. Produce to-dos a shop owner can act on this month; each to-do must cite one obligation quote verbatim. If you cannot ground a to-do in a quote, do not produce it. Output only the JSON schema."

## 6. Tavily usage — written so it reads as "best use"

Search is domain-filtered (`include_domains`, mode `filter`) so results are official sources only; `time_range` narrows to the crawl window; `include_raw_content=False` on search to save credits, then `extract(extract_depth="advanced")` on the deduped URL set, ≤20 per call. Per daily crawl for two regulations across nine domains: ~18 searches (2 credits each) + ~4 extract calls (2 credits per 5 URLs) ≈ 45 credits; the $25 credit grant covers the hackathon many times over. Log `usage` from every Tavily response and show "credits used this crawl" in the feed footer — judges for a "Best Use" bonus like seeing the integration measured.

## 7. Acceptance checklist before freeze

- Demo URL loads with the seeded run; feed shows both regulations; month view shows to-dos with dates and citations.
- Every to-do's citation link opens to the page containing the quoted sentence.
- Crawl job is idempotent (second run adds 0 changes).
- README's first screen names Token Factory, both Nemotron models, Serverless Endpoint + Job, and Tavily with the credit figure.
- MIT at top level; `.env` not committed; no merchant data in `examples/`.
