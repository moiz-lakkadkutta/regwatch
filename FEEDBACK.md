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
- (2026-10-01, first LIVE run, 21:31Z) `uv run pytest -m live` → 4 passed (57 s incl. docker build).
  Token Factory `/v1/models` lists both `nvidia/Nemotron-3-Ultra-550b-a55b` and `nvidia/Nemotron-3_5-Lightning`
  (25 models on the account). Tavily: advanced search on `eur-lex.europa.eu`, `max_results=3` → `usage.credits` 2;
  advanced extract of 1 URL → `usage.credits` **0** (documented as 2 per 5 URLs; a single URL appears to round down).
  Tavily `GET /usage` still reported `usage: 0` for the key right after two runs (~4 searches) — the dashboard
  counter lags the per-response `usage` field, so the per-call log is the reliable figure.
  Ultra classify of seed change-001 (withdrawal): decision `applies`, 3 to-dos all grounded, due 2026-06-19,
  no grounding retry, 0 dropped, 0 dates nulled; tokens 1,395 prompt / **1,054 completion** (offline fixture
  average was 218 completion — ~5× more live); $0.0046 at PLACEHOLDER $1.00/$3.00 per Mtok.
- (2026-10-01, second LIVE crawl after the crawl-scope fix, 22:08Z, branch `fix/crawl-scope`) `regwatch crawl --stage all`
  into a scratch DB: 9 min 07 s end to end (first run was cancelled after 11 min at 5/109 documents).
  Tavily: 14 searches + 3 extracts = **42 credits** (first run 72); 36 documents kept (first run 109). The allow-list path
  rule and language filter dropped apps/cdn/community.shopify.com, regional `www.shopify.com/xx/blog/…` and ~20
  foreign-language copies (all logged). **14 of 20 eur-lex `legal-content/…` URLs failed in `extract` with
  "Failed to fetch url"** (PDF/HTML/LSU variants); `eli/dir/2023/2673/oj` itself extracted fine.
  Lightning normalise: 36 documents · 9 ok first try · 3 retried · **24 failed** (invalid_json / schema) · $0.2616 at
  PLACEHOLDER prices. Cause: **53 of 63 calls returned exactly `completion_tokens=4096`** — the `max_tokens=4096` cap in
  `LiveLLMBackend` truncates Lightning's output mid-JSON. The offline fixtures never reached it (avg 217 completion
  tokens). The seed withdrawal document failed this way, so `examples/seed-run` was **not** re-recorded from this run.
- (2026-10-04, Lightning thinking) Root cause of the 4096-token failures: Nemotron 3.5 Lightning on Token Factory reasons by
  default and writes the trace ("Here's a thinking process: …") into `message.content` — `reasoning_content` stays
  None — so `response_format` json_schema is not honoured and the call ends `finish_reason=length` with
  `reasoning_tokens=4096`. Same prompt, one page (help.shopify withdrawal): system `/no_think` → still 4096 reasoning
  tokens (13.9 s); `reasoning_effort="low"` → same; `extra_body={"chat_template_kwargs": {"enable_thinking": false}}`
  → **262 completion tokens, valid JSON, 1.3 s**. Suggest documenting the switch on the model card and returning the
  trace in `reasoning_content`.
  Third live crawl (seed + `crawl --stage all`, 08:51Z): **3 min 22 s**, 44 Tavily credits, 40 documents;
  normalise 33/40 (18 first try, 15 retried, 7 dropped by the grounding check — 0 hit max_tokens), $0.1233;
  Ultra classify 33 changes → 15 apply / 14 don't / 4 unclear, 46 to-dos, 0 dropped, 0 failed, $0.1300
  (PLACEHOLDER prices). Quality gap: the live normalisation of `eli/dir/2023/2673/oj` summarised it as a
  financial-services directive (effective 2024-12-09) and Ultra correctly said `does_not` for a furniture shop; the
  withdrawal-function text starts at char 53,132 of 106,388 (input cap 60,000). Seed not re-recorded.
- (2026-10-04, fourth live crawl, 200k-char input cap) seed + `crawl --stage all`: 3 min 30 s, 40 Tavily credits,
  28 documents; normalise **27/28** (12 first try, 15 retried, 1 dropped by the grounding check, 0 at max_tokens),
  $0.1159; classify 27 → 13 apply / 11 don't / 3 unclear, 43 to-dos, 0 dropped, $0.1208 (PLACEHOLDER prices).
  The tr eur-lex copy was dropped and the `?locale=` duplicate collapsed. With the full text Lightning now dates
  Directive (EU) 2023/2673 correctly (2026-06-19, 4/4 runs) and lists the withdrawal-function obligation, but it
  still frames every obligation as "for financial services" (the act's title), so Ultra answers `does_not` for a
  furniture shop (5/5 runs). A prompt rule ("judge scope from the provision, not the title") changed nothing with
  thinking off (3/3 runs) and was reverted. Guidance pages (help.shopify, IHK) restating Art. 11a classify `applies`.
- (2026-10-06, act sections) Root cause of Directive (EU) 2023/2673 → `does_not`: one change per 106k-char amending act.
  Lightning thinking-on (32k max_tokens, 107 s, 17.9k reasoning tokens) and Ultra as extractor (25 s, 9.7k tokens;
  a second run hit 16k `finish_reason=length`) both still framed it as financial services. Normalising the Art. 11a
  amendment point alone → `applies` 2/2; with sections, the act-title instrument still flipped Ultra to `does_not`
  (0/2) until the stored instrument used the short id "Directive (EU) 2023/2673 — Article 1, point (3)" (2/2 applies).
  Fifth live crawl with per-section acts (seed + `crawl --stage all`, fresh DB): 8 min 03 s, 40 Tavily credits,
  29 documents → 60 changes (2023/2673: 10 of 12 sections; consolidated EUDR: 24 of 26); classify 25 apply /
  22 don't / 13 unclear, 105 to-dos, 0 dropped; $0.1715 normalise + $0.3568 classify (PLACEHOLDER prices).
  Art. 11a section → `applies`, 4 to-dos, due 2026-06-19; feed shows "7 other provisions of Directive (EU) 2023/2673
  don't apply to you".
- (2026-10-10, demo seed re-recorded) `examples/seed-run` is now recorded from the sixth live crawl (run6 DB,
  40 Tavily credits, $0.5283 model) by the new `regwatch record-seed` instead of hand: 4 URLs (2023/2673 /oj/eng,
  consolidated 2023/1115, help.shopify EU right of withdrawal, IHK Pfalz) with `--exclude` for the stale
  "Regulation (EU) 2023/1115 — Article 11 – Article 15" and "… — ANNEX II" sections →
  `record-seed: 4 documents · 29 changes · 7 skipped → examples/seed-run` (5 skipped for an undated to-do,
  2 excluded). Every quote is verbatim in the recorded document; the feed footer shows the crawl's 40 credits.
- (2026-10-10, prices) Token Factory catalog (`tokenfactory.nebius.com/model-catalog.md`, no date on the page):
  Nemotron 3.5 Lightning $0.06 in / $0.24 out per Mtok, Nemotron 3 Ultra $1.00 / $3.00 — our Lightning placeholders
  ($0.20 / $0.60) overstated normalise cost ~3×. Sixth live crawl re-priced from its usage log: Lightning 115 calls,
  682,890 / 58,210 tokens → $0.0549 (was shown as $0.1715); Ultra 61 calls, 102,279 / 84,830 → $0.3568; full crawl
  $0.4117 + 40 Tavily credits — ~87% of model spend is Ultra classify (one call per act section).
- (2026-10-10, Nebius CLI / AI Cloud) nebius CLI 0.12.287 on macOS arm64. `nebius profile create` is interactive only
  unless every value is passed as a flag; under a non-TTY it fails with `Error: ^D`. With flags
  (`--endpoint api.nebius.cloud --federation-endpoint auth.nebius.com --parent-id …`) the browser login worked, but
  the Token Factory project ID (`aiproject-e00…`) is not an AI Cloud project: `iam v2 project get` → "id must be a
  project id with 'project-' prefix"; with the prefix swapped → NotFound; `iam v2 tenant list` → `{}`;
  `registry list` → PermissionDenied "unknown subject". Token Factory access does not imply an AI Cloud tenant, and
  nothing in the Token Factory UI or the hackathon resources says so — the Serverless Endpoint/Job deploy is blocked
  until an AI Cloud tenant exists for the account.
- (2026-10-10, Serverless Endpoint deploy, eu-west2) After billing was set up on the new tenant, `nebius registry create`
  went from PermissionDenied to ACTIVE in ~2 s (`cr.eu-west2.nebius.cloud`); `registry configure-helper` + push of the
  342 MB linux/amd64 image worked first time. `nebius ai endpoint create … --platform cpu-d3 --preset 2vcpu-8gb --public
  --auth none --disk-size 32Gi --args serve` (validated with `--dry-run`): **257 s from create to RUNNING** (provision →
  image download → container start); the CLI blocks until RUNNING and prints a public tunnel URL plus a public IP.
  First requests: /healthz, /feed, /month all 200 in ~0.25 s; logs show `seeded 29 change(s)` then uvicorn.
  The smallest cpu-d3 preset in eu-west2 is 2vcpu-8gb (plan assumed 4vcpu-16gb). `--container-port 8000/http` from our
  notes is not the CLI's syntax (`8000`). The Serverless AI CLI has `--dry-run` — worth surfacing in the docs.
