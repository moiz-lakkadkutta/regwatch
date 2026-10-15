"""regwatch CLI: seed | record-seed | classify-presets | crawl | serve."""

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
    append: Annotated[
        bool,
        typer.Option(
            "--append", help="Add after the existing change files instead of replacing them."
        ),
    ] = False,
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
            settings,
            url,
            out if out is not None else settings.seed_dir,
            exclude=exclude or [],
            append=append,
        )
    except RecordError as e:
        print(f"record-seed: refused — {e}", file=sys.stderr)
        raise typer.Exit(code=1) from e
    print(summary.line(), file=sys.stderr)


@app.command("classify-presets")
def classify_presets(
    store: Annotated[
        list[str] | None,
        typer.Option("--store", help="Only this store slug (repeat for several; default: all)."),
    ] = None,
    seed_dir: Annotated[
        Path | None, typer.Option(help="Seed directory (default: REGWATCH_SEED_DIR).")
    ] = None,
    llm_backend: Annotated[str, typer.Option(help="Token Factory: live | fixtures")] = "live",
    llm_fixtures_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Classify every seed change for each example store that has no result yet (appends to
    stores/<slug>.applicability.jsonl; reruns only fill gaps)."""
    import logging

    from regwatch.config import Settings
    from regwatch.jobs.presets import classify_presets as _classify
    from regwatch.understand.llm_backend import make_llm_backend

    if llm_backend not in ("live", "fixtures"):
        raise typer.BadParameter("llm-backend must be 'live' or 'fixtures'")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = Settings()
    backend = make_llm_backend(
        "live" if llm_backend == "live" else "fixtures",
        settings,
        llm_fixtures_dir,
        stage="classify",
    )
    try:
        summary = _classify(settings, backend=backend, seed_dir=seed_dir, only=store or [])
    except ValueError as e:
        print(f"classify-presets: refused — {e}", file=sys.stderr)
        raise typer.Exit(code=1) from e
    for line in summary.lines():
        print(line, file=sys.stderr)


@app.command()
def crawl(
    stage: Annotated[str, typer.Option(help="all | ingest | normalize | classify")] = "all",
    backend: Annotated[str, typer.Option(help="Tavily: live | fixtures")] = "live",
    fixtures_dir: Annotated[Path | None, typer.Option()] = None,
    llm_backend: Annotated[str, typer.Option(help="Token Factory: live | fixtures")] = "live",
    llm_fixtures_dir: Annotated[Path | None, typer.Option()] = None,
    time_range: Annotated[
        str, typer.Option(help="day | week | month | year | any (no date filter)")
    ] = "year",
    regulation: Annotated[
        list[str] | None,
        typer.Option("--regulation", help="Only search this regulation tag (repeat for several)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(help="write the ingested documents.jsonl here (default: nothing is written)"),
    ] = None,
    no_out: Annotated[
        bool, typer.Option("--no-out", help="do not write documents.jsonl (the default)")
    ] = False,
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
    for tag in regulation or []:
        argv += ["--regulation", tag]
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
