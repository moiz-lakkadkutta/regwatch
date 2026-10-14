FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    REGWATCH_DB_URL=sqlite:////data/regwatch.db PORT=8000
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY regwatch ./regwatch
COPY examples ./examples
# `regwatch seed` reads Settings.seed_dir; pin it to the copied examples so the seed run is found
# whether the package is installed editable (below) or as a wheel into site-packages.
ENV REGWATCH_SEED_DIR=/app/examples/seed-run
# Dependencies come from uv.lock (the versions the tests ran against), then the project itself;
# editable keeps /app/regwatch next to /app/examples (the DEFAULT_SEED_DIR fallback).
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps -e . && mkdir -p /data
COPY ops/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=4)" || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
