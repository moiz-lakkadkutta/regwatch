# Status — Oct 24 freeze (offline)

**Days merged:** Oct 17 `83ce7ed` · 18 `2cec1c3` · 19 `f831474` · 20 `16474d0` · 21 `2266495` · 22 `9a02a38` · 23 `37a80bc` · 24 `ebe8246`.

**Since the freeze (2026-10-01 → 10-10, merged to `main`, pushed to github.com/moiz-lakkadkutta/regwatch):** live keys in
use (4/4 live tests pass) · crawl scope fix (allow-list path rule, en/de filter, content dedupe, concurrent normalise) ·
Lightning `enable_thinking=false` + 200k input cap · long EU acts normalised per article/amendment point (2023/2673
Art. 11a → applies) with the feed collapsing non-applying provisions · `regwatch record-seed` + demo seed recorded from
the sixth live crawl · Token Factory catalog prices set (Lightning $0.06 / $0.24, Ultra $1.00 / $3.00 per Mtok; a full
live crawl ≈ $0.41 + 40 Tavily credits). **Tests:** 289 passed, 4 live deselected.

**Endpoint cost approved by Moiz 2026-10-10 (~$8–15 through submission).** **Pending:** Endpoint + Job deploy (`docs/runbook-nebius.md`; `daily-crawl` workflow disabled until its secrets exist) ·
video (Oct 25–26) · clean-machine dry run (Oct 27) · Devpost submission (closes Oct 30, 10:00 PDT).

**Asks for Moiz:** keys into SecretStash `regwatch-keys` · one console session for runbook steps 1–10 · `REGWATCH_REPORT_EMAIL`.

**Top 5 open issues** (`docs/issues.md`): failed-normalize doc re-selected every crawl · `_todo_key` collision on shared quotes (one seed change has it) · unverified nebius CLI / `--args` syntax. The seed is recorded from a live crawl (`regwatch record-seed`: 4 documents, 29 changes, live Ultra applicabilities).

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
