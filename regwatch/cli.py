"""regwatch CLI: seed | crawl | serve."""

from __future__ import annotations

import sys
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


@app.command()
def crawl() -> None:
    """Search → extract → normalise → classify (the Serverless Job entrypoint)."""
    from regwatch.jobs.crawl import crawl as _crawl

    print(f"added {_crawl()} change(s)", file=sys.stderr)


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
