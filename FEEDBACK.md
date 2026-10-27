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
- (2026-10-10, Serverless Job, eu-west2) MysteryBox secret `regwatch-keys` created with
  `nebius mysterybox secret create --secret-version-payload '[{"key":…,"string_value":…}]'` (payload schema only in
  `--help`); Job env via `--env-secret NEBIUS_API_KEY=regwatch-keys` (selector = secret name, env var = payload key) —
  worked first time. Job #1 (`--dry-run` first; cpu-d3 2vcpu-8gb, 32 GiB, `--args crawl`): CLI blocked 531 s until
  COMPLETED; `ingest: 56 documents (52 new, 0 changed, 4 unchanged) · 52 Tavily credits · 14 searches · 4 extracts`,
  `normalize: 52 documents · 27 ok first try · 15 retried · 10 failed · $0.0501`, `classify: 47 changes · 19 apply ·
  21 don't · 7 unclear · 58 to-dos (0 dropped) · $0.1964`. `nebius ai job logs` returns only the last 100 lines by
  default, so the `seeded …` line was cut. Negative check (no `--env-secret`): FAILED after 204 s with
  `crawl: refusing to run — missing NEBIUS_API_KEY TAVILY_API_KEY`, CLI error "Job failed before startup completed"
  (code 9) — the message is misleading, the container did start and exit 2.
- (2026-10-14, agent, OFFLINE) "Let the agent check" on Nemotron 3 Ultra + Tavily basic, scripted replay
  (`tests/test_agent_loop.py`, not a live run): 4 steps (search · read · ask · decide), 2 Tavily credits, $0.0098 at
  the scripted 2,000 / 150 tokens per step. Worst-case reservation per Ultra call = 4,096 completion tokens x
  $3.00/Mtok = $0.0123 plus input at $1.00/Mtok, so the $1/day cap admits at most ~81 calls (~13 six-step runs)
  before input tokens. Tavily credits are settled as max(reported, 1) because a live single-URL advanced extract
  reported `usage.credits` 0 on 2026-10-01. Live steps, credits, USD and latency per step: see the LIVE entry below.
- (2026-10-14, agent, LIVE, `uv run pytest -m live -s tests/test_agent_live.py`, seed EUDR `unclear` change, 4-step
  cap) Run 1 (first prompt): 4 steps (read_source · search_sources · read_source · decide), 3 Tavily credits, $0.0202,
  3.8 s per step; it read pages for definitions, never asked the merchant, and stayed `unclear`. The first
  read_source of the 200-page act returned only its table of contents. Prompt fix: "Ask first" when the earlier
  reasons name a fact about the shop; don't read the change's own act for definitions. Run 2 (fixed prompt): 3 steps
  (ask_merchant "import or buy from an EU supplier?" · ask_merchant "company size?" · decide `applies`), 0 Tavily
  credits, $0.0170 (8,203 prompt / 2,606 completion tokens), 3.6 s per step. One extra run 1 was spent by mistake
  (re-run to print step lines): total live spend for this entry ≈ $0.057 + 6 Tavily credits. Tavily account before
  the runs: 549 of 1,000 plan credits used (`GET /usage`), so the Endpoint's agent cap is set to 24 credits/day.
- (2026-10-14, Nebius Serverless Endpoint redeploy with the agent) `nebius ai endpoint` (CLI 0.12.287) has create /
  delete / start / stop / restart but no `update`, so changing the image, env or secrets means a new endpoint and a
  new public URL (`regwatch-web` → `regwatch-web-2`, port8000-x8f9rz0wxq15br2 → port8000-skj2wff6z9w233q); every
  doc and the Devpost text carrying the URL had to change. Ask: an in-place update that keeps the tunnel URL.
  Create → RUNNING: 254 s. First attempt failed at startup ("Endpoint failed to start", code 9) — our bug: the
  Dockerfile installed dependencies unlocked, so the image got sqlmodel 0.0.48 / SQLAlchemy 2.1.4 (lock 0.0.42 /
  2.0.52), whose naive-datetime check crashed the agent's startup sweep; fixed by installing from `uv.lock`
  (`6292d2d`). `nebius ai endpoint logs` showed the traceback, which made this a 5-minute fix. Verified through the
  ingress: /healthz 200 in 0.18 s; a non-htmx same-origin agent POST → 303 (the Host-based Origin check works
  behind the tunnel); cross-origin → 403; locked profile POST → 403; the live agent's first step asked the merchant.
- (2026-10-15, Nebius Serverless Endpoint redeploy, small-company EUDR dates) No local Docker daemon, so the image
  was built by a manual GitHub Actions workflow (`publish-image`) and pushed to GHCR
  (`ghcr.io/moiz-lakkadkutta/regwatch:0d927fd`, public package, anonymous pull 200). The Endpoint pulled it with no
  registry credentials. `--dry-run` passed; create → RUNNING took 253 s (254 s on 10-14). Still no
  `nebius ai endpoint update`, so this is a third endpoint and a third public URL
  (`regwatch-web-3`, port8000-tbw06x74c5zg9z6). Verified through the ingress: /healthz, /month, /feed, /profile all
  200 in 0.57–0.78 s; logs show `seeded 26 change(s)`; /month has 14 rows due 2027-06-30 and none due 2026-12-30;
  locked profile POST → 403; agent POST cross-origin → 403, same-origin on the unclear change → 303. The agent's
  steps are not visible in `nebius ai endpoint logs` at the default log level.
- (2026-10-15, Tavily, rule-moved banner) One advanced extract of a single EUR-Lex URL (Regulation (EU) 2025/2650,
  `CELEX:32025R2650`) through the repo's `extract_documents` path was refused with `ForbiddenError: This request
  exceeds your plan's set usage limit` — 0 credits charged, no `usage` in the response. On 10-14 the account stood at
  549 of 1,000 plan credits, so a set usage limit below the plan size is the likely cause; the error does not say
  which limit or how many credits remain. Workaround: the banner quotes Article 38(2)/(3) from the consolidated
  text already in the seed (`CELEX:02023R1115-20251226`, which marks those paragraphs ▼M2 = 2025/2650). Ask: put
  the limit and the remaining credits in the 403 body.

- (2026-10-15, Tavily plan exhausted before a scoped crawl) Before a planned crawl of three new regulations
  (Directive (EU) 2024/825, Regulation (EU) 2025/40 PPWR, Regulation (EU) 2023/988 GPSR; ~20 search credits for
  10 advanced searches plus extracts, budget 150), `GET https://api.tavily.com/usage` returned
  `plan_usage 1001 / plan_limit 1000` on the Researcher plan (`search_usage 863`, `extract_usage 138`,
  `paygo_usage 0`, `paygo_limit null`). On 2026-10-14 the same endpoint showed 549 used, so ~452 credits went in
  about a day (not by this branch). One probe search (basic, max_results 1) then failed with
  `ForbiddenError: This request exceeds your plan's set usage limit`, so no crawl ran: 0 Tavily credits, $0 model
  spent. Ask: a warning (email / response header) at e.g. 80 % of the plan, and per-key usage history so a spike can
  be traced to the Endpoint agent, a Job or a developer key.

- (2026-10-15, Tavily, EUR-Lex URL forms for the EUDR wood exception) Four advanced extracts through the repo's
  `extract_documents` path, 4 credits in all (pay-as-you-go). `legal-content/EN/TXT/HTML/?uri=CELEX:32010R0995`,
  `…CELEX:32025R2650`, `…CELEX%3A32025R2650`, `…?uri=OJ:L_202502650`, `TXT/?uri=CELEX:32025R2650` and
  `…CELEX%3A02023R1115-20241226` all came back in `failed_results` with only `Failed to fetch url` — the first such
  call (2 URLs, both failed) was billed `usage.credits 1`, a later identical-looking one (2 URLs, both failed) was
  billed 0. The ELI forms worked: `eli/reg/2010/995/oj/eng` (74,824 chars, full Annex),
  `eli/reg/2025/2650/oj` (94,653 chars), `eli/reg/2024/3234/oj/eng` (40,818 chars),
  `eli/reg/2023/1115/2024-12-26/eng` (133,445 chars) — but `eli/reg/2025/2650/oj/eng` returned a 2,241-char page
  shell with no text (1 credit for that pair). Ask: a reason in `failed_results` (blocked, timeout, 4xx) and a
  documented rule for when a failed URL is billed.

- (2026-10-15, Tavily pay-as-you-go, scoped crawl of 2024/825 + 2025/40 + 2023/988) With pay-as-you-go enabled
  (`paygo_limit 10000`), `GET /usage` at 14:04 UTC read `plan_usage 1001 / 1000`, `search_usage 863`,
  `extract_usage 138`, `paygo_usage 0`; the `key` block equals the `account` block, so all 1,001 plan credits went
  through this one key — `/usage` has no per-day or per-caller history to find where ~450 of them went on 10-14/15.
  The crawl (`--regulation` ×3, `--time-range any`) then logged 10 advanced searches (20 credits) + 4 advanced
  extracts (8 + 4 + 7 + 4 = 23 credits) = 43 credits. `/usage` at 14:42 UTC still read `paygo_usage 0` and
  `plan_usage 1001`: the spend did not show up within ~35 minutes. Extract: 14 of 69 URLs came back
  `Failed to fetch url`, 12 of them in one batch of 20 (EUR-Lex `legal-content/…/TXT/HTML`, `/PDF`, `/NIM`, `/HIS`,
  `EN-FR`/`EN-IT` views, `eli/reg/2023/988/oj?initialSessionID=…`) that was still charged 4 credits for 8 results.
  Ask: per-request rows in `/usage` (or a usage export), and paygo usage that updates as fast as plan usage.
- (2026-10-15, Nemotron 3.5 Lightning, act metadata dates) Normalising Directive (EU) 2024/825 per article: the
  whole-act metadata call returned `effective_dates` 2026-03-27 + 2025-09-27 (the transposition deadline and the
  Commission's deadline for the harmonised notice) in two of three runs and never 2026-09-27, although Article 4(1)
  says "They shall apply those measures from 27 September 2026."; the classifier then dated merchant to-dos
  27 March 2026. The consolidated GPSR text (`eli/reg/2023/988/2026-05-29/eng`) gave sections dated 2026-05-29,
  the consolidation date. Those sections were left out of the seed. Lightning cost for the whole crawl: $0.1187
  (13 documents, first run 3/13 ok first try, 9 after the retry); Ultra classify $0.7759 for 107 classifications (84 + 4 + 4 + 15 over four runs).
- (2026-10-15, Nebius Serverless Endpoint redeploy, example stores + 3 new regulations) Image built by `publish-image`
  on GitHub (`ghcr.io/moiz-lakkadkutta/regwatch:817cd67`, anonymous manifest pull 200). `nebius ai endpoint create`
  outlived the local 120 s tool timeout but exited 0; the endpoint showed RUNNING after about 4 min. Still no
  `endpoint update`, so this is a fourth endpoint and URL (`regwatch-web-4`, port8000-pjh6xh7mwmhtxnp). Verified
  through the ingress: /healthz, /feed, /month, /profile 200 in 0.32–0.51 s; logs `seeded 33 change(s)`; each of the
  4 example stores shows 5 regulation groups and its own banner; de-furniture /month has 14 to-dos due 2026-12-30 and
  14 due 2027-06-30; locked profile POST → 403; cross-origin agent POST → 403.
- (2026-10-19, Nemotron 3 Ultra, live classify of the furniture store with the EUDR date notes) `classify-presets`
  on a scratch copy of the seed (store = the default DE furniture + leather profile, small company): 33 changes,
  35 calls, 72,631 prompt / 54,558 completion tokens, $0.2363, 2 min 35 s, 0 refused ungrounded. Ultra wrote one
  to-do per product group on its own: Articles 3, 5, 6–8, 9 and 10 give wooden furniture 2026-12-30 and leather
  2027-06-30 (the Art. 38(3) exception for 995/2010 products), matching the hand-split seed. Two differences from the
  seed: Articles 6–8 came out `applies` (seed: `unclear`, kept because the agent demo starts there) and the
  leather to-dos of Articles 17–24 had their 2027-06-30 nulled by code (not an effective date of that section).
- (2026-10-19, Nebius Serverless Endpoint redeploy, final URL) `nebius ai endpoint create` for `regwatch-web-5` failed
  with `ResourceExhausted … compute.instance.count (limit 2, requested 3)`: web-4 and an endpoint of another project
  held both instances. The error names the quota but the hint lines ("check the active profile", "configure
  authentication") point elsewhere. web-4 was deleted first (demo down for about 5 minutes); the second create
  exited 0 with the endpoint RUNNING. Still no `endpoint update`, so this is a fifth URL
  (port8000-fje4arxh07epk8b). Ask: an in-place image update, or a stable URL per endpoint name. Verified through
  the ingress: /healthz, /month, /feed, /profile, /change/12 200 in 0.34–1.34 s; 4 example stores 200; locked
  profile POST → 403; cross-origin agent POST → 403; the report link mails the owner. Change ids moved (the
  unclear EUDR Art. 6–8 is /change/12, was 14), so the runbook and the video script now use 12.
- (2026-10-27, Nebius Serverless Endpoint, key rotation) After the owner revoked the Token Factory key (no spend limit on
  the key), a new key went into a new primary version of the `regwatch-keys` MysteryBox secret. The endpoint reads secrets
  only at start, so the plan was `nebius ai endpoint restart aiendpoint-e04w2tksx7y5mzbvw8`, which failed with
  `FailedPrecondition: This Endpoint/Devlab is outdated. Please create a new one.` (request 77a499f5-b205-4dcf-bfd9-146f6b74e1b6),
  while the endpoint itself was RUNNING and serving 200s. So rotating a key meant a sixth endpoint and a sixth public URL
  (`regwatch-web-6`, port8000-jmda1596h1jt4g9): `--dry-run` first (it checks the instance quota, limit 2), delete web-5
  (1 min 44 s), create web-6 (exit 0, RUNNING, `seeded 33 change(s)`). Asks: a restart that works on any RUNNING endpoint, or
  secret versions that reach a running endpoint without one; a spend limit per Token Factory key.
