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
    # R1: `regwatch crawl --out <path>` exists (cli.py) — keep the live record off the baked seed.
    exec regwatch crawl --stage all --backend live --llm-backend live \
      --time-range "${REGWATCH_TIME_RANGE:-year}" --out /data/documents.jsonl ;;
  *) echo "entrypoint: unknown mode '$mode' (serve|crawl)" >&2; exit 2 ;;
esac
