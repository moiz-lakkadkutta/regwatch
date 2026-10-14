import pytest

from regwatch.agent.tools import (
    HEAD_CHARS,
    MAX_HITS,
    PAGE_VIEW_CHARS,
    WINDOW_CHARS,
    PageText,
    Refused,
    check_question,
    page_view,
    query_terms,
    read_source,
    search_sources,
    search_view,
)
from regwatch.ingest.tavily_backend import normalize_url
from tests.fakes import IHK_HIT, IHK_URL, ScriptedTavily

COMMISSION = "https://commission.europa.eu/eudr-guide"
FR_EURLEX = "https://eur-lex.europa.eu/legal-content/FR/TXT/?uri=CELEX:32023R1115"
SHOPIFY_DE = "https://www.shopify.com/de/blog/eudr"


@pytest.mark.parametrize(
    "query,domain",
    [
        ("EUDR micro", "example.com"),
        ("EUDR micro", None),
        ("EUDR micro", "www.ihk.de"),
        ("ab", "ihk.de"),
        ("x" * 201, "ihk.de"),
        (None, "ihk.de"),
    ],
)
def test_search_refused_before_any_tavily_call(query: str | None, domain: str | None) -> None:
    tavily = ScriptedTavily()
    with pytest.raises(Refused):
        search_sources(tavily, query=query, domain=domain, run_id=1)
    assert tavily.search_calls == []


def test_search_is_basic_depth_on_one_domain_with_credit_fallback() -> None:
    tavily = ScriptedTavily(results={"ihk.de": [IHK_HIT]}, search_credits=None)
    found = search_sources(tavily, query="  EUDR Händler  ", domain="IHK.DE", run_id=1)
    assert tavily.search_calls == [
        {
            "query": "EUDR Händler",
            "search_depth": "basic",
            "include_domains": ["ihk.de"],
            "include_domains_mode": "filter",
            "max_results": MAX_HITS,
            "include_usage": True,
        }
    ]
    assert found.credits == 1
    assert [h.url for h in found.hits] == [IHK_URL]


def test_search_uses_reported_credits_and_filters_hits() -> None:
    results = [
        {"title": "Off list", "url": "https://evil.example/eudr", "content": "x"},
        {"title": "French copy", "url": FR_EURLEX, "content": "x"},
        *[
            {"title": f"Hit {i}", "url": f"https://www.ihk.de/eudr-{i}", "content": "a" * 500}
            for i in range(7)
        ],
    ]
    tavily = ScriptedTavily(results={"ihk.de": results}, search_credits=2)
    found = search_sources(tavily, query="EUDR KMU", domain="ihk.de", run_id=1)
    assert found.credits == 2
    assert len(found.hits) == MAX_HITS
    assert all(h.source_domain == "ihk.de" and len(h.snippet) == 300 for h in found.hits)
    view = search_view(found.hits, first_hit_id=3)
    assert [v["hit_id"] for v in view] == [3, 4, 5, 6, 7]
    assert set(view[0]) == {"hit_id", "title", "url", "snippet"}


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://www.ihk.de/other-page", {normalize_url(IHK_URL)}),  # never shown in this run
        (None, {normalize_url(IHK_URL)}),
        ("https://evil.example/eudr", {normalize_url("https://evil.example/eudr")}),
        (
            "https://eur-lex.europa.eu@evil.example/eudr",  # userinfo: the host is evil.example
            {normalize_url("https://eur-lex.europa.eu@evil.example/eudr")},
        ),
        (FR_EURLEX, {normalize_url(FR_EURLEX)}),
        # normalize_url drops prefLang, so this matches the hit; the raw URL is a French copy
        (COMMISSION + "?prefLang=fr", {normalize_url(COMMISSION)}),
        # shopify.com is allow-listed only under /blog/
        (SHOPIFY_DE, {normalize_url(SHOPIFY_DE)}),
    ],
)
def test_read_refused_before_any_tavily_call(url: str | None, allowed: set[str]) -> None:
    tavily = ScriptedTavily(pages={IHK_URL: "page"})
    with pytest.raises(Refused):
        read_source(tavily, url=url, allowed_urls=allowed, run_id=1)
    assert tavily.extract_calls == []


@pytest.mark.parametrize("reported", [0, None])
def test_read_is_basic_depth_and_charges_at_least_one_credit(reported: int | None) -> None:
    tavily = ScriptedTavily(pages={IHK_URL: "# EUDR\n\ntext"}, extract_credits=reported)
    got = read_source(tavily, url=IHK_URL + "/", allowed_urls={normalize_url(IHK_URL)}, run_id=1)
    assert tavily.extract_calls == [
        {
            "urls": [normalize_url(IHK_URL)],
            "extract_depth": "basic",
            "format": "markdown",
            "include_usage": True,
        }
    ]
    assert got.credits == 1
    assert got.page == PageText(
        url=normalize_url(IHK_URL), source_domain="ihk.de", markdown="# EUDR\n\ntext"
    )


def test_read_drops_a_redirect_off_the_allow_list_after_the_call() -> None:
    tavily = ScriptedTavily(
        pages={IHK_URL: "page"}, redirect={normalize_url(IHK_URL): "https://evil.example/landing"}
    )
    got = read_source(tavily, url=IHK_URL, allowed_urls={normalize_url(IHK_URL)}, run_id=1)
    assert got.page is None and got.credits == 1 and "allow-list" in got.error


def test_read_without_content_is_an_error_not_a_page() -> None:
    allowed = {normalize_url(IHK_URL)}
    got = read_source(ScriptedTavily(), url=IHK_URL, allowed_urls=allowed, run_id=1)
    assert got.page is None and got.credits == 1 and got.error


def test_page_view_head_plus_three_windows_capped_at_10000() -> None:
    text = "x" * 7_000 + "alpha" + "y" * 15_000 + "bravo" + "z" * 15_000 + "charlie" + "w" * 15_000
    page = PageText(url=IHK_URL, source_domain="ihk.de", markdown=text)
    view = page_view(4, page, ["alpha", "bravo", "charlie", "zulu"])
    assert view["page_id"] == 4 and view["url"] == IHK_URL and view["chars"] == len(text)
    assert len(view["head"]) == HEAD_CHARS
    passages: list[str] = view["passages"]
    assert len(passages) == 3 and all(len(p) <= WINDOW_CHARS for p in passages)
    assert "alpha" in passages[0] and "bravo" in passages[1] and "charlie" in passages[2]
    assert len(view["head"]) + sum(len(p) for p in passages) == PAGE_VIEW_CHARS


def test_page_view_without_terms_is_the_head_only() -> None:
    view = page_view(1, PageText(url=IHK_URL, source_domain="ihk.de", markdown="a" * 9_000), [])
    assert len(view["head"]) == HEAD_CHARS and view["passages"] == []


def test_query_terms_are_distinct_letter_words() -> None:
    assert query_terms(["EU 2023/1115 Händler", "händler wood"]) == ["händler", "wood"]


@pytest.mark.parametrize(
    "question,options,n_asked",
    [
        ("Do you import goods yourself?", ["Yes", "No"], 2),
        ("Import?", ["Yes", "No"], 0),
        ("x" * 201, ["Yes", "No"], 0),
        ("Do you import goods yourself?", ["Yes"], 0),
        ("Do you import goods yourself?", ["a", "b", "c", "d", "e"], 0),
        ("Do you import goods yourself?", ["Yes", " "], 0),
        ("Do you import goods yourself?", ["Yes", "y" * 81], 0),
        ("Do you import goods yourself?", ["Yes", "Yes"], 0),
        ("Do you import goods yourself?", None, 0),
    ],
)
def test_question_bounds(question: str, options: list[str] | None, n_asked: int) -> None:
    with pytest.raises(Refused):
        check_question(question, options, n_asked=n_asked)


def test_question_ok_is_stripped() -> None:
    assert check_question("  Do you import goods yourself? ", [" Yes ", "No"], n_asked=1) == (
        "Do you import goods yourself?",
        ["Yes", "No"],
    )


def test_search_reporting_zero_credits_settles_at_one() -> None:
    tavily = ScriptedTavily(results={"ihk.de": [IHK_HIT]}, search_credits=0)
    assert search_sources(tavily, query="EUDR Händler", domain="ihk.de", run_id=1).credits == 1
