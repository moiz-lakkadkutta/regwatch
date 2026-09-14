# Status — Oct 24 freeze (offline)

**Days merged:** Oct 17 `83ce7ed` · 18 `2cec1c3` · 19 `f831474` · 20 `16474d0` · 21 `2266495` · 22 `9a02a38` · 23 `37a80bc` · 24 `ebe8246`.
**Tests:** 136 → 141 passed, 4 live deselected. No live call has been made; every credit/token/USD figure is fixture replay.

**Pending:** live keys · Endpoint + Job deploy (`docs/runbook-nebius.md`) · re-record `examples/seed-run` from a live crawl · video (Oct 25–26) · clean-machine dry run (Oct 27).

**Asks for Moiz:** `TAVILY_API_KEY` + `NEBIUS_API_KEY` (local `.env` + SecretStash `regwatch-keys`) · confirm the four model prices (`Settings`, PLACEHOLDER) · approve Endpoint cost above the $5 gate (~$8–15 for Oct 23–28) or pick the smallest preset · one console session for runbook steps 1–10 · `REGWATCH_REPORT_EMAIL`.

**Top 5 open issues** (`docs/issues.md`): failed-normalize doc re-selected every crawl · `_todo_key` collision on shared quotes · seed re-record from live · unverified nebius CLI / `--args` syntax · seed applicabilities only proven live by `test_classify_live.py`.

## Freeze checklist (Oct 24, worktree `day/1024`)

```
ruff check: All checks passed! · ruff format --check: 58 files already formatted · mypy regwatch: Success: no issues found in 18 source files
pytest -q: 141 passed, 4 deselected
git status --short: clean (after the final commit)
secret grep (TAVILY_API_KEY=|NEBIUS_API_KEY=, non-example, non-empty): 7 matches, all `--env-secret …=regwatch-keys`, `-e …=` or `NEBIUS_API_KEY=""` — 0 secrets
git ls-files .env: (none) · git check-ignore -v .env: .gitignore:1:.env
git ls-files examples/: examples/seed-run/change-001-withdrawal.json examples/seed-run/change-002-eudr.json examples/seed-run/documents.jsonl examples/seed-run/profile.json
head -40 README.md | grep -c "Token Factory|Nemotron-3-Ultra|Nemotron-3_5-Lightning|Serverless Endpoint|Serverless Job|Tavily|credits": 10
clean clone → uv sync --dev → regwatch --help → seed (2) → crawl --stage all fixtures (36 Tavily credits · $0.0345 model) → pytest (141 passed): real 7.255 s (warm uv cache)
```
