# CLAUDE.md — RegWatch

RegWatch tells a small EU merchant which regulation changes apply to their store, with a verbatim
quote and a source link for every to-do. Plan of record: `docs/regwatch-plan.md` (day-by-day, Oct 17–24).
UX rules: `docs/ux-brief.md` §3.

## Fixed decisions
- Python 3.12, FastAPI + Jinja2, SQLModel/SQLite. `uv`, `ruff`, `mypy --strict`, `pytest`.
- Token Factory models from env: CLASSIFIER = Nemotron 3 Ultra, EXTRACTOR = Nemotron 3.5 Lightning.
- Tavily is the only ingestion path; only `config.SOURCE_DOMAINS` are searched or extracted.
- Grounding is enforced in code: `RegChange.validate_quotes()` and `Applicability.ungrounded_todos()`
  must be empty before anything is stored. Never relax this in a prompt.
- `regwatch seed` must stay idempotent; `examples/seed-run` is what the demo URL shows on first boot.
- Copy: lead with what to do and by when; legal names second; "unclear" is a first-class state.

## Working rules
- `uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q` before "done".
- Log Tavily `usage` and model `usage` on every call; the feed footer shows credits per crawl.
- Friction with Nebius/NVIDIA/Tavily → dated entry in `FEEDBACK.md` with numbers.
