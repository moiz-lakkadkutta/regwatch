# Plan — record the demo seed from a live crawl (2026-10-10)

Branch `feat/record-seed`, worktree `../regwatch-seed`. Authority: CLAUDE.md ("`regwatch seed` must stay idempotent;
`examples/seed-run` is what the demo URL shows on first boot"; grounding enforced in code), `tests/test_seed_run.py`
invariants, docs/ux-brief.md §3.

## Global Constraints
- Python 3.12, `mypy --strict`, ruff. Done = `uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q` green in the worktree.
- Never call live APIs. The live crawl DB to record from already exists:
  `sqlite:////private/tmp/claude-502/-Users-moizp-regwatch/9aba7fc9-aa9b-4fe1-b04b-886f4bdb7aa1/scratchpad/run6/live.db`
  (read it; never write to it — copy it to a tmp path first if your code opens it read-write).
- Grounding: every recorded change must pass `RegChange.validate_quotes(<its document markdown>) == []` and its
  applicability `ungrounded_todos(change) == []`; the recorder refuses to write otherwise. Never relax in a prompt or test.
- `tests/test_seed_run.py` invariants stay as they are (do not weaken them): ≥ 2 change files; documents.jsonl URLs are
  sorted and equal the set of change sources; every to-do has a `due`; citation_url == document_url; host on allow-list.
- `regwatch seed` must load the new seed idempotently (second run adds 0) and the feed/month pages must render from it.

## Task 1: `regwatch record-seed` and the re-recorded demo seed

1. CLI command `regwatch record-seed --url URL [--url URL …] [--out DIR]` (default out = `Settings.seed_dir`), reading
   the DB from `Settings` (`REGWATCH_DB_URL`) like the other commands. Implementation in `regwatch/jobs/crawl.py` (or a
   new `regwatch/jobs/record.py` if cleaner) + `regwatch/cli.py`. Behaviour:
   - profile = current profile in the DB; `profile.json` in DIR is left untouched if present, written if absent.
   - For each URL: its `DocumentRow` → `documents.jsonl` (all chosen docs, sorted by url, same line format as
     `write_documents_jsonl`); every `RegChangeRow` for that url whose `content_hash` equals the document's current hash,
     with its `ApplicabilityRow` for the profile.
   - Skip, with a WARNING naming instrument + reason, any change that has no applicability, or whose applicability has a
     to-do with `due` None. If after skipping a URL has no changes left, drop the URL from documents.jsonl too (warn).
   - Write `change-NNN-<slug>.json` (NNN from 001 in (url, change id) order; slug = regulation tag if known else a short
     host-based slug; filesystem-safe), payload keys exactly as the existing seed files: `created_at` (change created_at
     ISO), `model` (applicability model), `cost_usd`, `tavily_credits`, `change` {document_url, instrument, jurisdiction,
     summary, effective_dates, obligations}, `applicability` {decision, reasons, todos}.
   - Before writing anything, delete existing `change-*.json` in DIR; then write all files; print one summary line
     ("record-seed: N documents · M changes · K skipped → DIR").
   - Refuse (non-zero exit, nothing written) if any chosen URL is missing from the DB or any change fails grounding.
2. Tests (offline, tmp DB built with existing helpers/fixtures): records the right files; skips no-due/no-applicability
   changes with warnings; refuses an ungrounded change; `seed()` into a fresh DB from the recorded dir reproduces the same
   changes/applicabilities and is idempotent; deterministic output (record twice → identical bytes).
3. Re-record the real seed into `examples/seed-run` from the run6 DB with exactly these URLs:
   - https://eur-lex.europa.eu/eli/dir/2023/2673/oj/eng?eliuri=eli%3Adir%3A2023%3A2673%3Aoj
   - https://eur-lex.europa.eu/legal-content/EN/TXT/HTML?uri=CELEX%3A02023R1115-20251226
   - https://help.shopify.com/en/manual/compliance/legal/eu-right-of-withdrawal
   - https://www.ihk.de/pfalz/recht/widerrufsbutton-kommt-ab-juni-2026-7003518
   - the eur-lex EUDR summary URL that starts with https://eur-lex.europa.eu/EN/legal-content/summary/fighting-deforestation (take the exact URL from the DB)
   Keep `examples/seed-run/profile.json`. Commit the regenerated files.
4. Update tests that hard-code the OLD seed (two hand-recorded changes for `eli/dir/2023/2673/oj` and `eli/reg/2023/1115/oj`,
   their instruments/summaries/to-do texts/counts) so they assert equivalent properties of the new seed — prefer
   properties (e.g. "the withdrawal group shows an applies item for Article 1, point (3) with a 2026-06-19 to-do", "the
   feed shows the '… other provisions of Directive (EU) 2023/2673 don't apply to you' line") over copying model text.
   `tests/test_classify_live.py` (live-only) must still point at a seed change that is the withdrawal Art. 11a section.
   Do not weaken `tests/test_seed_run.py`.
5. Update the runbook step that says how to re-record the seed (docs/runbook-nebius.md step 9) to use `record-seed`.
