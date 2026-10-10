<!-- fmt: off -->
# Runbook — RegWatch on Nebius Serverless AI (Endpoint + daily Job)

Live half of the Oct 23 step (`docs/plans/oct23.md` §6). Moiz is present; the orchestrator stops at
every **ASK** and never pastes a key. Values to enter are in `ops/serverless-endpoint.yaml` and
`ops/serverless-job.yaml`; the container entrypoint is `ops/entrypoint.sh` (`serve` | `crawl`).

Cost line: the Endpoint runs 24/7 — ~$0.05–0.10/h at 4 vCPU (≈ $1.2–2.4/day; RAM rate unverified)
+ 32Gi disk ≈ $2.3/month; Oct 23–28 ≈ $8–15 → above the $5 gate. Default disk is 250Gi
(≈ $17.75/month) — always lower it. One Job run ≈ 45 Tavily credits (~$0.36 of the $25 grant)
+ Lightning/Ultra tokens < $1 at placeholder prices + ~2 min CPU.

## Checklist

1. **Prereqs.** Docker Desktop running (`docker info` succeeds); install the nebius CLI
   (`curl -sSL https://storage.eu-north1.nebius.cloud/cli/install.sh | bash`); `nebius profile create`
   (browser login); note `PROJECT_ID`. Non-TTY: `nebius profile create regwatch --endpoint api.nebius.cloud
   --federation-endpoint auth.nebius.com --parent-id <project-…>` (an AI Cloud project, not the Token Factory `aiproject-…`).
   Deployed 2026-10-10: tenant `maroon-loon-tenant-k2d`, project `project-e04w3bxjbc0029g2wvxk5h` (eu-west2); billing must be
   set up before `registry create` (PermissionDenied otherwise).
2. **Registry.** `nebius registry create --name regwatch`; `nebius registry configure-helper` → note
   `REGISTRY_ID`.
3. **Build + push.**
   ```bash
   IMAGE=cr.$REGION.nebius.cloud/$REGISTRY_ID/regwatch:$(git rev-parse --short HEAD)   # deployed: cr.eu-west2.nebius.cloud/e04t3ertpv4dynst9y
   docker build --platform linux/amd64 -f ops/Dockerfile -t $IMAGE . && docker push $IMAGE
   ```
   Fallback if registry auth fails: push to public GHCR and use that image reference.
4. **Secret.** SecretStash secret `regwatch-keys` with keys `NEBIUS_API_KEY` and `TAVILY_API_KEY`
   (Moiz pastes the values; never the orchestrator).
5. **ASK (cost) — approved by Moiz 2026-10-10.** Endpoint runs 24/7 — ~$0.05–0.10/h at 4 vCPU (≈ $1.2–2.4/day; RAM rate
   unverified) + 32Gi disk ≈ $2.3/month; Oct 23–28 ≈ $8–15 → above the $5 gate. Moiz approves or picks
   the smallest listed preset. Then create the Endpoint with the `ops/serverless-endpoint.yaml` values
   (or its CLI line); note the time from Create to RUNNING (cold-start line for FEEDBACK); copy the
   public URL as `$URL`.
6. **Verify.** `curl -s $URL/healthz`; open `$URL` on a phone → month view / feed with both
   regulations; the footer shows the recorded seed state.
7. **Job #1.** Create with the `ops/serverless-job.yaml` values, `REGWATCH_TIME_RANGE=year`. Cost per
   run: ~45 Tavily credits (~$0.36 of the $25 grant) + Lightning/Ultra tokens < $1 at placeholder prices
   + ~2 min CPU. Expected logs: `seeded …` → `ingest: …` → `normalize: …` → `classify: …` →
   `crawl: …`; status COMPLETED.
   **Upgrading an existing volume (act sections release).** `documents_needing_normalize` skips any
   (url, hash) that already has a change, so a long EUR-Lex act stored whole is NOT re-split. The first
   crawl after this release must run against a fresh DB volume, or first delete the affected acts'
   rows (applicability first), e.g. for 2023/2673:
   `sqlite3 regwatch.db "DELETE FROM applicabilityrow WHERE regchange_id IN (SELECT id FROM regchangerow WHERE document_url LIKE '%eur-lex.europa.eu%2023/2673%'); DELETE FROM regchangerow WHERE document_url LIKE '%eur-lex.europa.eu%2023/2673%';"`
   (drop the `LIKE` filter to re-split every EUR-Lex act); the next crawl normalises them per section.
8. **Negative check (free).** Create a Job without `env_secret` → status FAILED with
   `crawl: refusing to run — missing NEBIUS_API_KEY TAVILY_API_KEY` in the logs (exit 2; fixtures are
   never used).
9. **Job #2 (idempotency acceptance).** Each Job gets a fresh /tmp DB seeded from the image, so "0 new" only holds
   once the seed contains everything the crawl finds; on 2026-10-10 Job #1 showed the seed documents as `4 unchanged`
   instead and Job #2 was not run. Expected `… (0 new, 0 changed, N unchanged) …` and
   `0 new changes · 0 classified`. If Job #1 found new documents: re-record the seed locally —
   crawl with keys into a scratch DB (`REGWATCH_DB_URL=sqlite:////tmp/live.db uv run regwatch crawl
   --stage all`; a crawl never writes `examples/seed-run`), check `/feed`, then
   `REGWATCH_DB_URL=sqlite:////tmp/live.db uv run regwatch record-seed --url <document URL> [--url …]
   [--exclude "<exact instrument>" …]` (writes `examples/seed-run`; keeps `profile.json`; skips
   changes with an undated to-do; refuses ungrounded changes and excludes that match nothing;
   carries the crawl's Tavily credits into the seed footer). Commit,
   rebuild/push, redeploy, repeat. Paste both summary lines into `FEEDBACK.md`.
10. **Schedule.** GitHub repository secrets `NEBIUS_SA_ID`, `NEBIUS_SA_PUBLIC_KEY_ID`,
    `NEBIUS_SA_PRIVATE_KEY`, `NEBIUS_PROJECT_ID`; repository variable `REGWATCH_IMAGE`; the workflow
    is disabled until then (2026-10-10, so the 05:00 UTC schedule does not fail daily) —
    `gh workflow enable daily-crawl`; run `daily-crawl` once via workflow_dispatch → a Job appears in
    the console. Cap: daily;
    `REGWATCH_TIME_RANGE=month`.
11. **Rollback.** Stop the Endpoint; disable the workflow; delete the Job; redeploy the previous image
    tag. Delete the Endpoint only after the video is recorded.

## Unverified until the console is open
- Whether the root disk survives Endpoint stop/start; whether one shared filesystem mounts rw on
  Endpoint + Job simultaneously (stretch: mount at `/data` on both).
- Whether an `--args`-only override works with an `ENTRYPOINT` image; fallback is
  `--container-command /app/entrypoint.sh --args crawl`.
- The RAM component of the CPU price and the minimum allowed `--disk-size`.
