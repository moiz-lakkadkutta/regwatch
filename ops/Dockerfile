FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY regwatch ./regwatch
COPY examples ./examples
RUN uv pip install --system .
ENV REGWATCH_DB_URL=sqlite:////data/regwatch.db
VOLUME ["/data"]
EXPOSE 8000
# Endpoint: seed then serve. Job: override CMD with `regwatch crawl`.
CMD ["sh", "-c", "regwatch seed && regwatch serve --port 8000"]
