# Spec — "Let the agent check": an agent that resolves `unclear` (2026-10-14)

Owner decisions (2026-10-14): approach A (own JSON tool loop on Nemotron 3 Ultra); runs **live** in the public web
app; global cap **$1 model + 60 Tavily credits per UTC day**; per run ≤ 6 model steps and ≤ 10 credits; agent
searches at Tavily `basic` depth. Independent review of the first draft: APPROVE-WITH-CHANGES; all of its blocking
and should-fix items are folded in below.

Goal: close the "pipeline, not an agent" gap for the hackathon video (Oct 25–26). Success = on the live URL, an
`unclear` change becomes `applies` / `does_not` (or a better-argued `unclear`) through visible steps, each tied to a
source quote or a fact the visitor gave, with its cost shown.

## 1. Invariants (unchanged, CLAUDE.md)

- To-dos are grounded by the existing code: the agent's `decide` is parsed into `ClassifiedPayload` and run through
  `enforce()` unchanged; `Applicability.ungrounded_todos()` must be empty before anything is stored.
- Tavily only on `config.SOURCE_DOMAINS`, checked **before** every call (the model now chooses domains and URLs).
- `regwatch seed` idempotent; the agent never writes `DocumentRow`, `RegChangeRow` or `ApplicabilityRow`.
- Every Tavily `usage` and model `usage` is logged and attributed to the run. Agent spend is NOT in the
  "credits per crawl" footer.

## 2. Components (`regwatch/agent/`)

| File | Responsibility |
|---|---|
| `schemas.py` | `AgentAction` (strict JSON schema the model returns), `Evidence`, `ReasonDraft`, `AgentDecision`, step result types |
| `tools.py` | `search_sources`, `read_source`, `ask_merchant` validation + execution; pre-call allow-list guards |
| `grounding.py` | `ground_decision(decision, change, pages, facts) -> (Applicability, AgentReasons, dropped)` |
| `budget.py` | per-run and per-day limits, reservation before each call, per-visitor rate limit |
| `loop.py` | `advance(run_id)`: rebuild context from stored steps, call Ultra, execute one tool, persist, repeat until wait/done/stop |
| `runner.py` | module-level `ThreadPoolExecutor(max_workers=2)`, `submit(run_id)`, startup sweep, expiry |
| `prompts/agent.md` | system prompt (tools, rules, output) |

### 2.1 Tables (`regwatch/db.py`)

- `AgentRunRow`: id, visitor_id (random 128-bit cookie `rw_visitor`, HttpOnly, SameSite=Lax), profile_id,
  regchange_id, status `queued|running|waiting|done|stopped|failed|expired`, stop_reason, decision (nullable),
  outcome_json (grounded Applicability + reason citations), n_steps, tavily_credits, cost_usd, created_at,
  updated_at.
- `AgentStepRow`: id, run_id, idx, tool, thought (≤ 300 chars), args_json, result_json (what the model is shown
  next, already truncated), credits, prompt_tokens, completion_tokens, cost_usd, created_at.
- `AgentPageRow`: id, run_id, url, source_domain, markdown (full, for verification), fetched_at. Never shared with
  `DocumentRow` (the reviewer's blocking item 1: URL is its primary key and a re-read would overwrite the seed's
  content hash and re-queue normalise).
- `AgentFactRow`: id, run_id, step_idx, question, options_json, chosen_index, chosen_text.

### 2.2 Tools (model-chosen; code validates every argument first)

- `search_sources(query, domain)`: `domain ∈ SOURCE_DOMAINS` else refused; query 3–200 chars. Tavily
  `search(search_depth="basic", include_domains=[domain], include_domains_mode="filter", max_results=5,
  include_usage=True)`. Results filtered with `allowed_domain` + `foreign_language` (as `search_regulation`) and
  stored in the step; the model sees `[{hit_id, title, url, snippet ≤ 300}]`. Credits = `usage.credits`, fallback 1.
- `read_source(url)`: URL must be one of this run's hit URLs or the change's `document_url` (normalised), and pass
  `allowed_domain` + `foreign_language`, else refused. Tavily `extract(urls=[url], extract_depth="basic",
  format="markdown", include_usage=True)`; credits = `max(1, usage.credits)` (a live single-URL extract once
  reported 0). Full markdown → `AgentPageRow`; the model sees `page_id` + the first 6,000 chars plus up to 3
  windows of 1,500 chars around the run's query terms, total ≤ 10,000 chars.
- `ask_merchant(question, options)`: question 10–200 chars; 2–4 options, each 1–80 chars. Run → `waiting`.
  The visitor answers by option **index**; free text is never accepted. At most 2 questions per run.
- `decide(decision, reasons, todos, evidence)`:
  - `todos`: `TodoDraft` list as in classify (quote must be one of the change's obligation quotes).
  - `evidence`: `[{evidence_id, page_id, quote}]`; each quote must be verbatim (whitespace-normalised substring)
    in that `AgentPageRow.markdown`, else dropped.
  - `reasons`: `[{text ≤ 300, fact_ids[], evidence_ids[], obligation_numbers[]}]`; a reason that cites nothing
    valid is dropped.
  - If the decision is `applies`/`does_not` and no reason with a valid citation remains → downgraded to
    `unclear` with the code-written reason "The agent could not back its answer with a source or your answer."
  - Then `ClassifiedPayload(decision, [r.text …], todos)` → `enforce(payload, change)`. Rejected to-dos get one
    retry (as classify; the retry counts as a step); still-rejected are dropped and logged.
  - A refused or invalid action is returned to the model as an error result and counts as a step.

### 2.3 Budget and abuse controls (`budget.py`, `Settings`)

- Settings (env): `REGWATCH_AGENT_ENABLED` (default `false`), `REGWATCH_AGENT_DAILY_USD` (1.0),
  `REGWATCH_AGENT_DAILY_CREDITS` (60), `REGWATCH_AGENT_RUN_STEPS` (6), `REGWATCH_AGENT_RUN_CREDITS` (10),
  `REGWATCH_AGENT_VISITOR_RUNS_PER_HOUR` (5), `REGWATCH_PROFILE_LOCKED` (default `false`).
- Day = UTC date; spend = sum over `AgentStepRow` created that day (`cost_usd`, `credits`), so a run that spans
  midnight is split correctly.
- Reservation: before a model call, require remaining day USD ≥ worst-case call cost (estimated prompt tokens ×
  in-price + 4,096 × out-price); before a Tavily call, require ≥ 1 credit left for both run and day. Otherwise the
  run stops with `stop_reason="daily budget used"` / `"run budget used"`.
- At most 2 runs execute at once (executor size); one active (`queued|running|waiting`) run per visitor+change;
  5 new runs per visitor per hour. POST routes require `HX-Request: true` or a same-origin `Origin`/`Referer`.
- Kill switch: `REGWATCH_AGENT_ENABLED=false` hides the button and refuses POSTs (404).
- `REGWATCH_PROFILE_LOCKED=true` (set on the demo) makes `/profile` read-only with a note, so one visitor cannot
  re-key the shared profile and blank the demo (reviewer: `POST /profile` hides every card for everyone).

### 2.4 Runtime

- One uvicorn worker (`cli.py serve`); more workers are unsupported and documented as such.
- `submit(run_id)` puts `advance(run_id)` on the executor. `advance` first does a conditional update
  `UPDATE agentrunrow SET status='running' WHERE id=? AND status IN ('queued')`; 0 rows → return (double-click
  safe). Each step opens its own `Session`; SQLite engine gets `check_same_thread=False` (already needed by FastAPI
  threads — verify) and short transactions.
- Context is rebuilt from stored steps every time (no thread waits for an answer). Answer → fact row, status
  `waiting → queued` (conditional), `submit`.
- App startup: `running|queued` → `failed` ("interrupted by restart"). `waiting` older than 30 min → `expired`
  when read.
- Model call: existing `LLMBackend.complete(model=classifier_model, response_format=strict AgentAction schema)`;
  `max_tokens` 4,096 as today. Invalid JSON → error result to the model, counts as a step.

### 2.5 Model contract (`AgentAction`, strict schema, all fields required, unused = null)

`{thought: str, tool: "search_sources"|"read_source"|"ask_merchant"|"decide", query, domain, url, question,
options, decision, reasons, todos, evidence}`. The prompt gives: store profile, the change (instrument,
summary, numbered obligations with quotes, effective dates), the classifier's `unclear` reasons, the allowed
domains, facts answered so far, and the trace (tool, args, result) so far; rules: prefer asking the merchant when
the missing fact is about the store, prefer searching when it is about the law; never claim a fact not given; the
last allowed step must be `decide`.

## 3. UI (Section 2)

- htmx moves from `month.html` to `base.html` (pinned `htmx.org@2.0.4`, already used).
- `/change/{id}` with decision `unclear` and agent enabled: a card under the unclear reasons: "Let the agent check —
  it searches the official sources and may ask you one or two questions" + button (POST
  `/change/{id}/agent` → 303 / fragment).
- Trace fragment `GET /agent/{run_id}` (only for the cookie's visitor): steps as a list —
  "Searched **ihk.de** for "…" — 5 results · 1 credit", "Read **eur-lex.europa.eu/…** · 1 credit",
  "Asked you: … → *your answer*", "Decided: **applies**"; each with the one-line thought in muted text; a run
  line "N steps · N credits · $0.0xx". Polls `hx-trigger="every 1.5s"` only while `queued|running`; the final
  response omits the trigger so polling stops.
- `waiting`: the question with one button per option (POST `/agent/{run_id}/answer`, `option=<index>`).
- `done`: an overlay card above the original: pill "Agent: applies" (or doesn't / unclear), reasons each followed
  by its citations ("you told us: …" / quote + source link), to-dos with quote + link as on the existing card, and
  "Only you see this; the shared demo is unchanged." The original card stays below, labelled "Original check".
- `stopped|failed|expired`: one line with the reason, trace kept, "Try again" if budget allows.
- A visitor's latest finished run for (profile, change) is shown on reload; runs of an old profile_id are ignored.
- Copy follows the UX brief: what to do and by when first, legal names second.

## 4. Testing and deploy (Section 3)

Tests (offline, scripted stub LLM + fake Tavily; gate stays green):
- tools: off-list domain refused before any Tavily call; URL not in hits refused; foreign-language refused;
  basic depth + credits fallback; truncation sizes.
- grounding: evidence not verbatim dropped; reason citing nothing dropped; applies without valid citation →
  unclear; to-dos still go through `enforce()` (non-obligation quote rejected, one retry, then dropped); a
  monkeypatched leak is never stored (mirror `test_classify_all_refuses_leaked_ungrounded_todo`).
- budget: per-run steps/credits stop; daily USD/credits stop; reservation refuses a call that would overshoot;
  visitor hourly limit; one active run per visitor+change.
- loop/runtime: replay after answer yields the same context; double `advance` runs once; startup sweep; expiry;
  invalid JSON and refused tools count as steps; last step forced to `decide`.
- routes: button only on unclear + enabled; kill switch 404; Origin check; answer accepts index only; another
  visitor's run is 404; polling stops on done; profile lock.
- seed untouched: after an agent run, `DocumentRow`/`ApplicabilityRow` counts and hashes unchanged; `regwatch seed`
  still idempotent.
- live (`-m live`, manual, ≤ $0.05 + ≤ 10 credits): one real run on the seed's EUDR `unclear` change.

Deploy (needs the owner's go at that step):
- `ops/serverless-endpoint.yaml` + runbook: add `env_secret` NEBIUS_API_KEY/TAVILY_API_KEY = regwatch-keys,
  `REGWATCH_AGENT_ENABLED=true`, `REGWATCH_PROFILE_LOCKED=true`; this reverses "no keys on the public VM" —
  say why (owner decision, capped).
- Prefer updating the existing Endpoint in place; if it must be recreated and the URL changes, update README,
  `docs/devpost.md`, `docs/status.md`, the yaml and `tests/test_readme.py` before the video.
- Check the Tavily plan's remaining monthly credits first (60/day × 16 days could reach ~960).
- FEEDBACK.md: dated entry with agent numbers (steps, credits, USD, latency per step).

## 5. Out of scope

Agent in the crawl Job; facts reused across runs or written into the profile; multi-worker; NeMo Agent Toolkit;
streaming; GPU hosting.
