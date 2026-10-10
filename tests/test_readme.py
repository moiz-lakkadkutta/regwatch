"""README truth constraints: the credit and search figures it quotes are derived from the fixture
ingest (so they cannot drift from the code), every offline or live figure is labelled as such in the
same sentence, the stack is named above the fold, and no marketing words slip in."""

from __future__ import annotations

from pathlib import Path

from regwatch.config import Settings
from regwatch.ingest.tavily_backend import make_backend
from regwatch.jobs.crawl import crawl

README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_quotes_the_fixture_crawl_figures(tmp_settings: Settings) -> None:
    run = crawl(
        tmp_settings, stage="ingest", backend=make_backend(tmp_settings, "fixtures"), out=None
    )
    text = README.read_text(encoding="utf-8")
    lines = text.splitlines()
    head = "\n".join(lines[:40])

    assert f"{run.tavily_credits} Tavily credits" in text
    assert f"{run.n_search_calls} searches" in text
    assert f"{run.n_extract_calls} extract" in text
    for needle in (
        "Token Factory",
        tmp_settings.classifier_model,
        tmp_settings.extractor_model,
        "Serverless Endpoint",
        "Serverless Job",
        "Tavily",
        "credits",
    ):
        assert needle in head, f"{needle!r} must appear in the first 40 lines"

    credit_lines = [ln for ln in lines if "Tavily credits" in ln and "36" in ln]
    assert credit_lines
    for ln in credit_lines:
        low = ln.lower()
        assert "offline" in low or "fixture" in low or "pending" in low, ln
    assert any("measured offline" in ln and "Tavily credits" in ln for ln in lines)
    usd_lines = [ln for ln in lines if "$0.0" in ln]
    assert usd_lines
    for ln in usd_lines:
        assert "live" in ln.lower() or "offline" in ln.lower(), ln

    assert "AI-powered" not in text and "we think" not in text.lower()
    assert "tunnel.applications." in text and "Serverless Endpoint" in text
    assert "> Status:" not in text
    assert text.startswith("# RegWatch\n")
