"""regwatch CLI: seed | record-seed | crawl | serve."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="regwatch", no_args_is_help=True, add_completion=False, rich_markup_mode=None
)


@app.command()
def seed() -> None:
    """Load the recorded seed run so the app is never empty."""
    from regwatch.jobs.crawl import seed as _seed

    n = _seed()
    print(f"seeded {n} change(s)", file=sys.stderr)


@app.command("record-seed")
def record_seed(
    url: Annotated[
        list[str], typer.Option("--url", help="Document URL to record (repeat for several).")
    ],
    out: Annotated[
        Path | None, typer.Option(help="Seed directory (default: REGWATCH_SEED_DIR).")
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Exact instrument to leave out (repeat for several)."),
    ] = None,
) -> None:
    """Record chosen documents of a crawl in the DB (REGWATCH_DB_URL) as the demo seed."""
    import logging

    from regwatch.config import Settings
    from regwatch.jobs.record import RecordError
    from regwatch.jobs.record import record_seed as _record

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    settings = Settings()
    try:
        summary = _record(
            settings, url, out if out is not None else settings.seed_dir, exclude=exclude or []
        )
    except RecordError as e:
        print(f"record-seed: refused — {e}", file=sys.stderr)
        raise typer.Exit(code=1) from e
    print(summary.line(), file=sys.stderr)


@app.command()
def crawl(
    stage: Annotated[str, typer.Option(help="all | ingest | normalize | classify")] = "all",
    backend: Annotated[str, typer.Option(help="Tavily: live | fixtures")] = "live",
    fixtures_dir: Annotated[Path | None, typer.Option()] = None,
    llm_backend: Annotated[str, typer.Option(help="Token Factory: live | fixtures")] = "live",
    llm_fixtures_dir: Annotated[Path | None, typer.Option()] = None,
    time_range: Annotated[str, typer.Option()] = "year",
    out: Annotated[
        Path | None,
        typer.Option(help="documents.jsonl path (default: examples/seed-run for --backend live)"),
    ] = None,
    no_out: Annotated[bool, typer.Option("--no-out", help="do not write documents.jsonl")] = False,
) -> None:
    """Search → extract → diff → normalise → classify; the Serverless Job entrypoint."""
    from regwatch.jobs.crawl import STAGES, main

    if stage not in STAGES:
        raise typer.BadParameter(f"stage must be one of {', '.join(STAGES)}")
    for name, value in (("backend", backend), ("llm-backend", llm_backend)):
        if value not in ("live", "fixtures"):
            raise typer.BadParameter(f"{name} must be 'live' or 'fixtures'")
    argv = ["--stage", stage, "--backend", backend, "--llm-backend", llm_backend]
    argv += ["--time-range", time_range]
    if fixtures_dir is not None:
        argv += ["--fixtures-dir", str(fixtures_dir)]
    if llm_fixtures_dir is not None:
        argv += ["--llm-fixtures-dir", str(llm_fixtures_dir)]
    if out is not None:
        argv += ["--out", str(out)]
    if no_out:
        argv.append("--no-out")
    rc = main(argv)
    if rc != 0:
        raise typer.Exit(code=rc)


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
