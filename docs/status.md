# Status — 2026-10-19

**Live:** https://port8000-fje4arxh07epk8b.tunnel.applications.eu-west2.nebius.cloud — Serverless Endpoint `regwatch-web-5` (cpu-d3 2vcpu-8gb, eu-west2), image
`ghcr.io/moiz-lakkadkutta/regwatch:f01e14d`, deployed 2026-10-19 and kept up through judging (Dec 1–15, 2026).
web-4 was deleted first (the account's instance quota is 2; the other one is `warden-console`). Agent on
($1 + 24 Tavily credits per day, shared `regwatch-keys`); `REGWATCH_REPORT_EMAIL` set.

**Decisions (owner, 2026-10-19):** host through Dec 15 · current keys and caps for the agent · no daily schedule
(the workflow stays disabled) · report email = the owner's address. The Tavily key is shared with another
project, which accounts for the plan credits not spent by RegWatch.

**Since web-4:** PR #8 review fixes deployed · fr-coffee Article 33 to-dos undated (were 2024-12-30) · reference
tag survives a re-extract · live check that Ultra splits wood 2026-12-30 / leather 2027-06-30 by itself ($0.2363) ·
Devpost text rewritten. **Tests:** 492 passed, 5 live deselected.

**Pending (owner):** phone check of the live URL · video (Oct 25–26) · clean-machine dry run (Oct 27) · Devpost
submission (target Oct 28; closes Oct 30, 10:00 PDT) · confirm the official rules.

## History — Oct 24 freeze (offline)

**Days merged:** Oct 17 `83ce7ed` · 18 `2cec1c3` · 19 `f831474` · 20 `16474d0` · 21 `2266495` · 22 `9a02a38` · 23 `37a80bc` · 24 `ebe8246`.

**Since the freeze (2026-10-01 → 10-10, merged to `main`, pushed to github.com/moiz-lakkadkutta/regwatch):** live keys in
use (4/4 live tests pass) · crawl scope fix (allow-list path rule, en/de filter, content dedupe, concurrent normalise) ·
Lightning `enable_thinking=false` + 200k input cap · long EU acts normalised per article/amendment point (2023/2673
Art. 11a → applies) with the feed collapsing non-applying provisions · `regwatch record-seed` + demo seed recorded from
the sixth live crawl · Token Factory catalog prices set (Lightning $0.06 / $0.24, Ultra $1.00 / $3.00 per Mtok; a full
live crawl ≈ $0.41 + 40 Tavily credits). **Tests:** 289 passed, 4 live deselected.

**Endpoint cost approved by Moiz 2026-10-10 (~$8–15 through submission).** **Live:** https://port8000-pjh6xh7mwmhtxnp.tunnel.applications.eu-west2.nebius.cloud (Serverless Endpoint `regwatch-web-4`,
cpu-d3 2vcpu-8gb, eu-west2, image `ghcr.io/moiz-lakkadkutta/regwatch:817cd67` (built by the `publish-image` workflow), redeployed 2026-10-15 (example stores, rule-move banner, EUDR wood exception, green claims + PPWR + GPSR: 33 changes) with "Let the agent check" on: keys from
MysteryBox, agent cap $1 + 24 Tavily credits/day). **Job #1 (2026-10-10):** COMPLETED in Nebius with keys from MysteryBox `regwatch-keys` — 52 new documents, 47 changes,
52 Tavily credits, $0.25 model; a Job without the secret FAILED with `crawl: refusing to run` (exit ≠ 0).
**Pending:** daily schedule (service account + 4 repo secrets; `daily-crawl` workflow disabled) ·
video (Oct 25–26) · clean-machine dry run (Oct 27) · Devpost submission (closes Oct 30, 10:00 PDT).

**2026-10-15, three more regulations crawled live and in the seed:** `config.SEED_REGULATIONS` has
`greenclaims` (Directive (EU) 2024/825), `packaging` (Regulation (EU) 2025/40, PPWR) and `productsafety`
(Regulation (EU) 2023/988, GPSR) on eur-lex / commission / ihk.de (+ help.shopify.com for GPSR); `applies_from`
verified against the crawled acts (quotes in `config.py`). Scoped live crawl (`--regulation` ×3 `--time-range any`,
Tavily pay-as-you-go): 10 searches + 4 extracts = 43 credits, 52 documents (14 extract failures, all eur-lex or a PDF);
13 kept for normalising; model spend $0.8946 for the default store + $0.1499 for `classify-presets` (21 calls).
`record-seed --append` added 7 changes from 5 documents (change-027…033; change-001…026 byte-identical):
greenclaims 2 (IHK guidance on 2024/825 — applies, due 27 Sep 2026; Commission Q&A — unclear), packaging 2
(PPWR Art. 24 empty space — applies, due 12 Feb 2028 / 1 Jan 2030; IHK PPWR guidance — unclear), productsafety 3
(GPSR Art. 4–8 and Art. 19–21 — apply, in force since 13 Dec 2024; Art. 11 importers — unclear). Section splitter
fixed for ELI pages that write `Article\xa0N` (no-break space) and for a headless annex of an amending act. Left out:
2024/825 act sections (the act-level date step picked the 27 March 2026 transposition / 27 Sep 2025 Commission
deadlines as merchant due dates, twice), the consolidated GPSR text (to-dos dated 29 May 2026 = consolidation date),
IHK Frankfurt (instrument named "Richtlinie (EU) 2024/1772", not in the page). **Tests:** 466 passed, 5 live deselected.

**Asks for Moiz:** decide on the daily schedule (Tavily free plan 1000 credits/month vs ~52 per run; results do not reach the Endpoint) · `REGWATCH_REPORT_EMAIL` · check the live URL on a phone.

**Top 5 open issues** (`docs/issues.md`): failed-normalize doc re-selected every crawl · `_todo_key` collision on shared quotes (one seed change has it) · unverified nebius CLI / `--args` syntax. The seed is recorded from live crawls (`regwatch record-seed`: 9 documents, 33 changes — 13 apply, 16 don't, 4 unclear, 41 to-dos — live Ultra applicabilities; EUDR to-dos due 30 June 2027 for a small company; 83 Tavily credits across the two crawls).

## Freeze checklist (Oct 24, worktree `day/1024`)

```
ruff check: All checks passed! · ruff format --check: 58 files already formatted · mypy regwatch: Success: no issues found in 18 source files
pytest -q: 141 passed, 4 deselected
git status --short: clean (after the final commit)
secret grep (TAVILY_API_KEY=|NEBIUS_API_KEY=, non-example, non-empty): 7 matches, all `--env-secret …=regwatch-keys`, `-e …=` or `NEBIUS_API_KEY=""` — 0 secrets
git ls-files .env: (none) · git check-ignore -v .env: .gitignore:1:.env
git ls-files examples/: examples/seed-run/change-001…029-{withdrawal,eudr}.json examples/seed-run/documents.jsonl examples/seed-run/profile.json (re-recorded 2026-10-10 with `regwatch record-seed`; was two hand-recorded changes at the freeze)
head -40 README.md | grep -c "Token Factory|Nemotron-3-Ultra|Nemotron-3_5-Lightning|Serverless Endpoint|Serverless Job|Tavily|credits": 10
clean clone → uv sync --dev → regwatch --help → seed (2) → crawl --stage all fixtures (36 Tavily credits · $0.0345 model) → pytest (141 passed): real 7.255 s (warm uv cache)
```
