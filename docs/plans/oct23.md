<!-- fmt: off -->
# Oct 23 plan — Endpoint + Job on Nebius Serverless AI

Orchestrator notes R1–R6 (override the plan where they conflict):
- R1: `regwatch crawl --out` — check `cli.py`/`main()` accept `--out <path>`; if `--out` exists use `--out /data/documents.jsonl`, else `--no-out`. Report which.
- R2: Do NOT touch `regwatch/app.py` (`/`→`/month` is Oct 22's). The CI docker smoke: add the two steps to ci.yml exactly as the plan says; they run only on GitHub's Ubuntu runner.
- R3: The `daily-crawl.yml` workflow must be a no-op that fails fast with a clear notice when the four secrets are unset (use a first step that checks `${{ secrets.NEBIUS_SA_ID }}` and `exit 1` with `::error::` if empty). Never falls back to fixtures.
- R4: `tests/test_ops.py::test_entrypoint_crawl_invokes_live_stage_all` uses a stub `regwatch` script on PATH; make sure the stub dir is FIRST on PATH and `bash` is resolved from the real PATH; run with `cwd` = repo root of the worktree (use `Path(__file__).parents[1]`).
- R5: README: add a short "Deploy on Nebius" section with the data-story sentence (verbatim from plan §2) and the three commands (build, endpoint, job as CLI lines) — a stub; Oct 24 writes the full section. Update the status line.
- R6: FEEDBACK.md: add the Oct 23 template line with the offline facts only (no cron on Jobs; no verified persistent volume; 250Gi default disk; docker daemon needed locally), leaving `__` blanks for live numbers.

Non-negotiables (CLAUDE.md + plan):
- Endpoint = same image, `serve` mode (`regwatch seed && regwatch serve`); Job = same image, `crawl` mode running `regwatch crawl --stage all --backend live --llm-backend live`; missing key → exit 2 with a one-line reason, never fixtures.
- `.env` never in the image; `.dockerignore` excludes `.env`, `*.db`, `.git`, `.venv`, `tests/`.
- No merchant data in the image beyond examples/seed-run.
- Only config.SOURCE_DOMAINS searched (unchanged code path).
- Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy regwatch && uv run pytest -q`. Docker daemon is DOWN on this Mac — do not try `docker build`; the `-m live` docker test must skip cleanly when `docker info` fails.

---

## 1. Nebius facts found (docs read 2026-09-14)
- Serverless AI = container-over-VM **Endpoints** (persistent HTTP) and **Jobs** (run-to-completion). Public preview. Per-second billing while running. https://docs.nebius.com/serverless/overview · https://nebius.com/blog/posts/introducing-serverless
- Endpoint create: console / `nebius ai endpoint create` / REST. Required: name, image (`registry/path:tag`), platform+preset, container port (`--container-port 8000/http` → managed HTTPS URL). `--auth none|token` (default none = public). Optional `--container-command`, `--args`, `--env K=V`, `--env-secret K=<SecretStash selector>`, `--volume <fs|s3://bucket>:/path[:rw]`, `--disk-size` (default **250Gi**). https://docs.nebius.com/serverless/endpoints/manage · https://docs.nebius.com/cli/reference/ai/endpoint/create · https://docs.nebius.com/serverless/quickstart/endpoints (CPU platform `cpu-d3`, preset `4vcpu-16gb`).
- **No replicas / autoscaling / scale-to-zero / request-timeout / health-probe flags**; an Endpoint is one VM running until stopped; stopped endpoints aren't billed (volumes are).
- Job create: same image/env/secret/volume flags; `--timeout` 1h–168h; `--restart-policy never|on-failure`; non-zero exit → `FAILED`; logs via console or `nebius ai job logs <id> --follow`. https://docs.nebius.com/serverless/jobs/manage · https://docs.nebius.com/cli/reference/ai/job/create
- **No schedule/cron field on Jobs.** "Daily" must come from outside: GitHub Actions `schedule:` calling `nebius ai job create` with a service-account profile. https://docs.nebius.com/cli/configure
- Persistent storage: only via `--volume` (shared filesystem or S3 mount). Whether the root disk survives stop/start and whether one filesystem mounts rw on Endpoint + Job simultaneously: **unverified — check in console**.
- Registry: `nebius registry create --name regwatch`; `nebius registry configure-helper`; push to `cr.eu-north1.nebius.cloud/<registry-id>/regwatch:<tag>`; public GHCR also accepted. https://docs.nebius.com/container-registry/quickstart.md
- Secrets: SecretStash (`nebius mysterybox secret create …`); `--env-secret TAVILY_API_KEY=<secret>`. https://docs.nebius.com/mysterybox/secrets/create.md
- Pricing: CPU $0.012/vCPU-h + RAM (unverified) ; network SSD $0.071/GiB-month → 250Gi default ≈ $17.75/month: set disk-size to the minimum allowed (try 32Gi). https://docs.nebius.com/compute/resources/pricing
- CLI install: `curl -sSL https://storage.eu-north1.nebius.cloud/cli/install.sh | bash` then `nebius profile create`. https://docs.nebius.com/cli/install
- Local: `docker` 28.5.1 present, daemon not running; Mac is arm64 → `--platform linux/amd64`; `nebius` CLI not installed.

## 2. Storage decision + README data story
Endpoint = `seed && serve` on its own ephemeral SQLite; Job = same image, `crawl` entrypoint, its own ephemeral SQLite seeded first then crawled live (so "second run adds nothing new" is a real property and no key sits on the public VM). Stretch: if the console shows a shared filesystem mountable rw on both, mount at `/data` on both.
README sentence (verbatim): "The public URL serves the recorded run in `examples/seed-run` (re-recorded from a live crawl before freeze); the daily Serverless Job runs the same image with the live crawl entrypoint against a freshly seeded database and proves in its logs that a second pass adds nothing — live results reach the URL by re-recording the seed and rebuilding the image, not by a shared database, because Serverless Endpoints have no persistent volume we could verify."

## 3. Files — as in the orchestrator's allowed list.
Allowed: `ops/Dockerfile`, `ops/entrypoint.sh` (new, chmod +x, commit the mode bit), `.dockerignore` (new), `ops/serverless-endpoint.yaml`, `ops/serverless-job.yaml` (new), `.github/workflows/ci.yml`, `.github/workflows/daily-crawl.yml` (new), `tests/test_ops.py` (new), `.env.example`, `README.md`, `FEEDBACK.md`, `docs/issues.md`, `docs/plans/oct23.md`, `docs/runbook-nebius.md`. Forbidden: `regwatch/**`, `examples/**`, `tests/**` other than test_ops.py, `pyproject.toml`, `uv.lock`.

## 4. Contents
**ops/Dockerfile**
```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    REGWATCH_DB_URL=sqlite:////data/regwatch.db PORT=8000
WORKDIR /app
COPY pyproject.toml README.md ./
COPY regwatch ./regwatch
COPY examples ./examples
RUN uv pip install --system --no-cache . && mkdir -p /data
COPY ops/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz',timeout=4)" || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
```
(Check the uv image tag exists in your knowledge; if unsure use `ghcr.io/astral-sh/uv:latest` and note it.)
**.dockerignore**: `.git`, `.venv`, `.env`, `.env.*`, `*.db`, `*.db-journal`, `tests/`, `docs/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `__pycache__/`, `dist/`, `.github/`, `FEEDBACK.md`, `.DS_Store`. (`ops/` must NOT be ignored.)
**ops/entrypoint.sh**
```bash
#!/usr/bin/env bash
# RegWatch container entrypoint. Modes: serve (Endpoint) | crawl (Job). Exit 2 = configuration error.
set -euo pipefail
mode="${1:-serve}"
export REGWATCH_DB_URL="${REGWATCH_DB_URL:-sqlite:////data/regwatch.db}"
db_path="${REGWATCH_DB_URL#sqlite:///}"; mkdir -p "$(dirname "$db_path")" 2>/dev/null || true
case "$mode" in
  serve)
    regwatch seed
    exec regwatch serve --host 0.0.0.0 --port "${PORT:-8000}" ;;
  crawl)
    missing=(); [ -n "${NEBIUS_API_KEY:-}" ] || missing+=(NEBIUS_API_KEY); [ -n "${TAVILY_API_KEY:-}" ] || missing+=(TAVILY_API_KEY)
    if [ "${#missing[@]}" -gt 0 ]; then
      echo "crawl: refusing to run — missing ${missing[*]} (live keys required; fixtures are never used in the Job)" >&2; exit 2; fi
    regwatch seed
    exec regwatch crawl --stage all --backend live --llm-backend live \
      --time-range "${REGWATCH_TIME_RANGE:-year}" --out /data/documents.jsonl ;;   # R1: or --no-out
  *) echo "entrypoint: unknown mode '$mode' (serve|crawl)" >&2; exit 2 ;;
esac
```
**ops/serverless-endpoint.yaml** — header: `# Nebius Serverless AI has no declarative YAML apply as of 2026-09; these are the values to enter in console (Serverless AI → Endpoints → Create) or the flags for the CLI line at the bottom.`
```yaml
name: regwatch-web
image: cr.eu-north1.nebius.cloud/<REGISTRY_ID>/regwatch:<GIT_SHA>
platform: cpu-d3
preset: 4vcpu-16gb          # pick the smallest listed
container_port: 8000/http
auth: none
public_ip: false
disk_size: 32Gi             # default 250Gi costs ~$17.75/month
container_command: ""
args: "serve"
env: {PORT: "8000", REGWATCH_DB_URL: "sqlite:////data/regwatch.db"}
env_secret: {}
# cli: nebius ai endpoint create --name regwatch-web --image $IMAGE --platform cpu-d3 --preset 4vcpu-16gb --container-port 8000/http --auth none --disk-size 32Gi --args serve --env PORT=8000
```
**ops/serverless-job.yaml** — same header; name `regwatch-crawl`, same image/platform/preset, `args: "crawl"`, `timeout: 1h`, `restart_policy: never`, `disk_size: 32Gi`, `env:` every non-secret var from `.env.example` (TOKENFACTORY_BASE_URL, REGWATCH_CLASSIFIER_MODEL, REGWATCH_EXTRACTOR_MODEL, the four REGWATCH_*_PRICE_* placeholders, REGWATCH_NORMALIZE_MAX_CHARS, REGWATCH_TIME_RANGE: year, REGWATCH_REPORT_EMAIL if present), `env_secret: {NEBIUS_API_KEY: regwatch-keys, TAVILY_API_KEY: regwatch-keys}`, `schedule: "see .github/workflows/daily-crawl.yml — Nebius Jobs have no cron field"`; cli line: `nebius ai job create --name regwatch-crawl-$(date +%F) --image $IMAGE --platform cpu-d3 --preset 4vcpu-16gb --args crawl --timeout 1h --env REGWATCH_TIME_RANGE=month --env-secret NEBIUS_API_KEY=regwatch-keys --env-secret TAVILY_API_KEY=regwatch-keys`.
**.github/workflows/daily-crawl.yml**: `on: {schedule: [{cron: "0 5 * * *"}], workflow_dispatch: {}}`; steps: guard (R3), install CLI, write `${{ secrets.NEBIUS_SA_PRIVATE_KEY }}` to a file, `nebius profile create --endpoint api.nebius.cloud --service-account-id ${{ secrets.NEBIUS_SA_ID }} --public-key-id ${{ secrets.NEBIUS_SA_PUBLIC_KEY_ID }} --private-key-file key.pem --profile ci --parent-id ${{ secrets.NEBIUS_PROJECT_ID }}`, then the job CLI line with `IMAGE=${{ vars.REGWATCH_IMAGE }}`, `REGWATCH_TIME_RANGE=month`.
**ci.yml** add after pytest: `docker build --platform linux/amd64 -f ops/Dockerfile -t regwatch:ci .` and `docker run --rm -e NEBIUS_API_KEY= -e TAVILY_API_KEY= regwatch:ci crawl; test $? -eq 2`.
**.env.example**: add `REGWATCH_TIME_RANGE=year` and `PORT=8000` with comments.

## 5. Tests — `tests/test_ops.py`
- `test_dockerfile_basics`: base `python:3.12-slim`, `EXPOSE 8000`, `HEALTHCHECK`, `ENTRYPOINT ["/app/entrypoint.sh"]`, `CMD ["serve"]`, no `.env` COPY.
- `test_dockerignore_excludes_secrets_and_dbs`: lines `.env`, `*.db`, `.git`, `.venv`, `tests/` present; `ops/` absent.
- `test_entrypoint_crawl_exits_2_without_keys` (parametrized: both missing, only NEBIUS missing, only TAVILY missing): `subprocess.run(["bash","ops/entrypoint.sh","crawl"], env={"PATH": os.environ["PATH"], "REGWATCH_DB_URL": f"sqlite:///{tmp}/t.db"}, cwd=repo)` → rc 2, stderr contains `missing` and the key names.
- `test_entrypoint_crawl_invokes_live_stage_all`: stub `regwatch` script first on PATH appending argv to a file and exiting 0; with both keys set assert calls are `seed` then `crawl --stage all --backend live --llm-backend live --time-range year …`; with `REGWATCH_TIME_RANGE=month` assert `month`; stub exiting 3 → entrypoint rc 3.
- `test_entrypoint_unknown_mode_exits_2`.
- `test_ops_yaml_mirror_env_example`: every `REGWATCH_*`/`TOKENFACTORY_*` key in `.env.example` appears in `ops/serverless-job.yaml`; `NEBIUS_API_KEY`/`TAVILY_API_KEY` appear only under `env_secret`; endpoint YAML has empty `env_secret`.
- `test_daily_workflow_guards_secrets`: parse `.github/workflows/daily-crawl.yml` as text: has `schedule:` cron, `workflow_dispatch`, a guard step referencing `secrets.NEBIUS_SA_ID`, and no `fixtures` string.
- `test_docker_build_and_healthz` `@pytest.mark.live` (skipped unless `docker info` succeeds): build, run `serve` on a free port, poll `/healthz` ≤60s, `GET /` → 303, `GET /feed` contains `2023/2673`.

## 6. RUNBOOK (Moiz present; orchestrator stops at every "ASK") — write to docs/runbook-nebius.md
1. Prereqs: Docker Desktop running; install nebius CLI; `nebius profile create` (browser login); note PROJECT_ID.
2. Registry: create `regwatch`; `nebius registry configure-helper` → REGISTRY_ID.
3. Build+push: `IMAGE=cr.eu-north1.nebius.cloud/$REGISTRY_ID/regwatch:$(git rev-parse --short HEAD)`; `docker build --platform linux/amd64 -f ops/Dockerfile -t $IMAGE . && docker push $IMAGE`. Fallback: public GHCR.
4. Secret: SecretStash `regwatch-keys` with keys `NEBIUS_API_KEY`, `TAVILY_API_KEY` (Moiz pastes; never the orchestrator).
5. **ASK (cost)**: Endpoint runs 24/7 — ~$0.05–0.10/h at 4vCPU (≈$1.2–2.4/day; RAM rate unverified) + 32Gi disk ≈ $2.3/month; Oct 23–28 ≈ $8–15 → above the $5 gate; Moiz approves or picks the smallest preset. Then create the Endpoint with `ops/serverless-endpoint.yaml` values; note time to RUNNING (cold-start line for FEEDBACK); copy the public URL.
6. Verify: `curl -s $URL/healthz`; open `$URL` on a phone → month view / feed with both regulations; footer shows the seed state.
7. Job #1: create with `ops/serverless-job.yaml` values, `REGWATCH_TIME_RANGE=year`. Cost per run: ~45 Tavily credits (~$0.36 of the $25 grant) + Lightning/Ultra tokens < $1 at placeholder prices + ~2 min CPU. Expected logs: `seeded …` → `ingest: …` → `normalize: …` → `classify: …` → `crawl: …`; status COMPLETED.
8. Negative check (free): Job without `env_secret` → FAILED with `crawl: refusing to run — missing NEBIUS_API_KEY TAVILY_API_KEY`.
9. Job #2 (idempotency acceptance): expected `… (0 new, 0 changed, N unchanged) …` and `0 new changes · 0 classified`. If Job #1 found new documents, re-record the seed locally (`uv run regwatch crawl --stage all` with keys → `examples/seed-run/documents.jsonl`), rebuild/push, redeploy, repeat. Paste both summary lines into FEEDBACK.
10. Schedule: GitHub secrets `NEBIUS_SA_ID`, `NEBIUS_SA_PUBLIC_KEY_ID`, `NEBIUS_SA_PRIVATE_KEY`, `NEBIUS_PROJECT_ID`; variable `REGWATCH_IMAGE`; run `daily-crawl` via workflow_dispatch once → a Job appears in the console. Cap: daily; `REGWATCH_TIME_RANGE=month`.
11. Rollback: stop the Endpoint; disable the workflow; delete the Job; redeploy previous image tag. Delete the Endpoint only after the video is recorded.

## 7. Risks — cold start; no scale-to-zero (always-on cost > $5 gate); Job/Endpoint DB split (README sentence + re-record seed before freeze); registry auth (GHCR fallback); `uv pip install --system .` unpinned + arm64 → `--platform linux/amd64`; verify the wheel ships `regwatch/prompts/*.md` (hatch includes package data by default — add a test that `importlib.resources.files("regwatch.prompts")` lists both .md files); `--args`-only override unverified → fallback command `/app/entrypoint.sh`; 250Gi default disk.

## 8. Commits
1. `feat(oct23): ops/entrypoint.sh serve|crawl modes (exit 2 without live keys), Dockerfile HEALTHCHECK + entrypoint, .dockerignore, tests/test_ops.py`
2. `feat(oct23): ops/serverless-endpoint.yaml + serverless-job.yaml (console values), daily-crawl workflow (schedule via nebius CLI), ci docker build + exit-2 smoke`
3. `docs(oct23): README deploy stub with data-story sentence, FEEDBACK Oct 23 template, issues`

## 9. FEEDBACK.md Oct 23 template
`- (Oct 23) Nebius Serverless AI: Endpoint created in __ console steps (__ min from Create to RUNNING; IMAGE_PULLING __ s; first /healthz at __ s). Job: __ steps, __ min run, exit __, Tavily __ credits, model $__. Idempotency: run 2 printed "__ (0 new, 0 changed, N unchanged)". Friction (found offline 2026-09-14): (1) Jobs have no schedule/cron field — daily run needs GitHub Actions + a service-account profile; (2) no persistent volume documented for Endpoints, so the Job cannot write the Endpoint's SQLite (design note in README); (3) no scale-to-zero/replicas — always-on ~$__/day; default 250Gi disk = $17.75/month unless lowered; (4) __ (registry/CLI).`
