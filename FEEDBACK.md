# Feedback on Nebius, NVIDIA and Tavily tooling

Dated entries with numbers. **Measured:** everything marked OFFLINE (fixture replay of recorded
Tavily/model replies, 141 tests). **Pending live keys:** every credit, token, USD, latency and
console-step figure; the Oct 23 entry is a template with `__` blanks, filled on the live day.

- (Oct 18) Tavily, `tavily-python 0.8.2`: `TavilyClient(api_key=None)` does not raise — it silently
  goes keyless and still issues the HTTP request. Without a key on the dev machine this would have
  turned every test run into a network call, so `LiveTavilyBackend.__init__` refuses an empty key
  (`RuntimeError`) and all 39 tests run against a fixture backend. Suggest the SDK fail fast.
- (Oct 18) Tavily, offline fixture arithmetic for one ingest run over 2 regulations × their
  allow-listed domains: 14 advanced searches × 2 credits = 28, plus 1 advanced extract of 16 URLs
  (2 credits per 5 URLs → 8) = **36 credits per ingest**; estimated from documented pricing, live
  figure pending `TAVILY_API_KEY` (`uv run pytest -m live -s tests/test_ingest_live.py` prints it).
- (Oct 19) Nemotron 3.5 Lightning via Token Factory, normalisation of 16 documents (5 official, 11 guidance,
  5 German) — OFFLINE fixture replay, live figures pending `NEBIUS_API_KEY`:
  quote-fidelity rate (obligations whose quote was verbatim on first try) = 20/22 (91%); documents needing a
  retry = 2 (1 fixed by the retry, 1 still ungrounded); documents dropped after retry = 1; obligations
  dropped = 1. Tokens: 17,330 prompt / 3,900 completion over 18 calls; $0.0058 at $0.20/$0.60 per Mtok
  (prices: PLACEHOLDER defaults in `Settings`, confirm on the Token Factory pricing page).
  `response_format` json_schema strict: pending live (the live backend retries once without it on a 400).
  Grounding is enforced in code (`RegChange.validate_quotes` on the full page text) — the prompt cannot
  loosen it; `uv run pytest -m live tests/test_llm_backend.py` is the smoke test once a key exists.
- (Oct 20) Nemotron 3 Ultra via Token Factory, classify stage. OFFLINE fixture replay: 6/6 labels (3 apply / 2 don't / 1 unclear);
  ungrounded to-dos before retry 1, after retry 0; invented dates nulled 1; tokens/call prompt 1,638 · completion 218
  (26,200 / 3,490 over 16 calls, 15 changes + 1 retry); $0.0367 per 16-change crawl at PLACEHOLDER $1.00/$3.00 per Mtok
  (`Settings.classifier_price_*`, confirm on the Token Factory pricing page). Grounding is enforced in code
  (`Applicability.ungrounded_todos` at the store boundary, `citation_url` and `due` set by us) — the prompt cannot loosen it.
  Live accuracy, response_format strict acceptance (anyOf on `due`), reasoning-trace size and latency: pending
  NEBIUS_API_KEY (`uv run pytest -m live -s tests/test_classify_live.py` prints usage and cost).
- (Oct 23) Nebius Serverless AI: Endpoint created in __ console steps (__ min from Create to RUNNING; IMAGE_PULLING __ s;
  first /healthz at __ s). Job: __ steps, __ min run, exit __, Tavily __ credits, model $__. Idempotency: run 2 printed
  "__ (0 new, 0 changed, N unchanged)". Friction (found offline 2026-09-14): (1) Jobs have no schedule/cron field — daily
  run needs GitHub Actions + a service-account profile (`.github/workflows/daily-crawl.yml`); (2) no persistent volume
  documented for Endpoints, so the Job cannot write the Endpoint's SQLite (design note in README); (3) no
  scale-to-zero/replicas — always-on ~$__/day; default 250Gi disk = $17.75/month unless lowered (we set 32Gi);
  (4) __ (registry/CLI); (5) local: the image cannot be built or smoke-tested without a running Docker daemon
  (`docker info` failed on the dev Mac, arm64 → `--platform linux/amd64`), so the exit-2 smoke runs in CI and
  `uv run pytest -m live tests/test_ops.py` skips locally.
- (Oct 24) Freeze, offline. Top frictions so far, with numbers: (1) Tavily `tavily-python 0.8.2` accepts
  `api_key=None` and still sends HTTP — one guard line in `LiveTavilyBackend.__init__` is what keeps 141 tests
  network-free; ask: fail fast in the SDK. (2) Nebius Serverless Jobs have no schedule field — replacing one
  cron line took a 5-step GitHub Actions workflow, 4 repository secrets and 1 variable
  (`.github/workflows/daily-crawl.yml`). (3) No documented persistent volume for Serverless Endpoints and no
  confirmed rw filesystem shared with a Job — so the daily Job cannot write the Endpoint's SQLite; the demo
  URL is refreshed by re-recording the seed and rebuilding (README "Deploy"); default 250Gi disk ≈
  $17.75/month unless lowered to 32Gi. (4) Our own packaging bug: `jobs/crawl.py` resolved the seed dir as
  `Path(__file__).parents[2]/examples/seed-run`, which points into `site-packages` in a non-editable install —
  fixed Oct 24 with `Settings.seed_dir` (`REGWATCH_SEED_DIR`, Dockerfile sets `/app/examples/seed-run`). No
  live numbers exist yet.
