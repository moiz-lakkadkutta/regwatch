FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    REGWATCH_DB_URL=sqlite:////data/regwatch.db PORT=8000
WORKDIR /app
COPY pyproject.toml README.md ./
COPY regwatch ./regwatch
COPY examples ./examples
# Editable install: `regwatch.jobs.crawl.SEED_DIR` is resolved relative to the package file
# (`parents[2]/examples/seed-run`), so the package must stay at /app/regwatch next to /app/examples.
RUN uv pip install --system --no-cache -e . && mkdir -p /data
COPY ops/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=4)" || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
