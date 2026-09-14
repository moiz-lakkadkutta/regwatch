"""regwatch CLI: seed | crawl | serve."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from regwatch.jobs.crawl import DEFAULT_OUT

app = typer.Typer(
    name="regwatch", no_args_is_help=True, add_completion=False, rich_markup_mode=None
)


@app.command()
def seed() -> None:
    """Load the recorded seed run so the app is never empty."""
    from regwatch.jobs.crawl import seed as _seed

    n = _seed()
    print(f"seeded {n} change(s)", file=sys.stderr)


@app.command()
def crawl(
    stage: Annotated[str, typer.Option(help="ingest | all")] = "all",
    backend: Annotated[str, typer.Option(help="live | fixtures")] = "live",
    fixtures_dir: Annotated[Path | None, typer.Option()] = None,
    time_range: Annotated[str, typer.Option()] = "year",
    out: Annotated[Path, typer.Option(help="documents.jsonl path")] = DEFAULT_OUT,
) -> None:
    """Search → extract → diff (normalise → classify from Oct 19); the Serverless Job entrypoint."""
    from regwatch.config import Settings
    from regwatch.ingest.tavily_backend import make_backend
    from regwatch.jobs.crawl import crawl as _crawl

    if backend not in ("live", "fixtures"):
        raise typer.BadParameter("backend must be 'live' or 'fixtures'")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = Settings()
    tavily = make_backend(settings, cast(Literal["live", "fixtures"], backend), fixtures_dir)
    try:
        summary = _crawl(settings, stage=stage, backend=tavily, time_range=time_range, out=out)
    except NotImplementedError as e:
        print(f"crawl: {e}", file=sys.stderr)
        raise typer.Exit(code=2) from e
    print(summary.line(), file=sys.stderr)


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "0.0.0.0",
    port: Annotated[int, typer.Option()] = 8000,
) -> None:
    """Run the web app."""
    import uvicorn

    uvicorn.run("regwatch.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
