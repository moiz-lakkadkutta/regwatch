"""Live Tavily smoke test. Manual only: `uv run pytest -m live -s tests/test_ingest_live.py`.

Spends real credits (one advanced search ≈ 2, one advanced extract of 1 URL ≈ 2).
"""

import pytest

from regwatch.config import SEED_REGULATIONS, Settings
from regwatch.ingest.tavily_backend import CreditLedger, LiveTavilyBackend, is_allowed

pytestmark = pytest.mark.live


def test_live_search_and_extract_report_usage() -> None:
    pytest.importorskip("tavily")
    settings = Settings()
    if not settings.tavily_api_key:
        pytest.skip("TAVILY_API_KEY not set")
    backend = LiveTavilyBackend(settings.tavily_api_key)
    ledger = CreditLedger()
    query = next(r["query"] for r in SEED_REGULATIONS if r["tag"] == "withdrawal")

    search = backend.search(
        query=query,
        search_depth="advanced",
        include_domains=["eur-lex.europa.eu"],
        include_domains_mode="filter",
        time_range="year",
        max_results=3,
        include_raw_content=False,
        include_usage=True,
    )
    assert "usage" in search, search.keys()
    assert search["results"]
    assert all(is_allowed(r["url"]) for r in search["results"]), [
        r["url"] for r in search["results"]
    ]
    search_credits = ledger.add_search(search)

    first = search["results"][0]["url"]
    extract = backend.extract(
        urls=[first], extract_depth="advanced", format="markdown", include_usage=True
    )
    assert "usage" in extract, extract.keys()
    assert extract["results"]
    assert all(is_allowed(r["url"]) for r in extract["results"])
    extract_credits = ledger.add_extract(extract, 1)

    print(
        f"credits: search={search_credits} extract={extract_credits} "
        f"(usage: search={search['usage']} extract={extract['usage']})"
    )
