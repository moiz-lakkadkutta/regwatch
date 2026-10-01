import logging
import math

import pytest

from regwatch.config import SEED_REGULATIONS, SOURCE_DOMAINS
from regwatch.ingest.search import Hit, dedupe_hits, search_all, search_regulation
from regwatch.ingest.tavily_backend import (
    CreditLedger,
    LiveTavilyBackend,
    allowed_domain,
    foreign_language,
)
from tests.fakes import RecordingBackend

WITHDRAWAL = next(r for r in SEED_REGULATIONS if r["tag"] == "withdrawal")
EUR_LEX_2673 = "https://eur-lex.europa.eu/eli/dir/2023/2673/oj"
TRUSTEDSHOPS = "https://www.trustedshops.de/ratgeber/widerrufsbutton/"


def test_search_passes_filter_mode_and_params(fake_backend: RecordingBackend) -> None:
    search_regulation(fake_backend, "withdrawal", WITHDRAWAL["query"], ledger=CreditLedger())
    assert fake_backend.search_calls
    for kw in fake_backend.search_calls:
        assert kw["include_domains_mode"] == "filter"
        assert kw["search_depth"] == "advanced"
        assert kw["max_results"] == 10
        assert kw["include_raw_content"] is False
        assert kw["include_usage"] is True
        assert kw["time_range"] == "year"
        assert len(kw["include_domains"]) == 1
        assert kw["include_domains"][0] in SOURCE_DOMAINS
        assert kw["query"] == WITHDRAWAL["query"]
    assert [kw["include_domains"][0] for kw in fake_backend.search_calls] == [
        d for d, tags in SOURCE_DOMAINS.items() if "withdrawal" in tags
    ]


def test_search_drops_offlist_hits(
    fake_backend: RecordingBackend, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="regwatch.ingest.search"):
        hits = search_regulation(
            fake_backend, "withdrawal", WITHDRAWAL["query"], ledger=CreditLedger()
        )
    assert all(h.source_domain in SOURCE_DOMAINS for h in hits)
    assert not any("trustedshops" in h.url for h in hits)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(TRUSTEDSHOPS in r.getMessage() for r in warnings)


def test_dedupe_merges_tags_and_strips_utm(fake_backend: RecordingBackend) -> None:
    hits = search_all(fake_backend, ledger=CreditLedger())
    urls = [h.url for h in hits]
    assert urls == sorted(urls)
    assert len(urls) == len(set(urls))
    assert not any("utm_" in u or "#" in u for u in urls)
    by_url = {h.url: h for h in hits}
    assert by_url[EUR_LEX_2673].regulation_tags == ("withdrawal",)
    both = "https://help.shopify.com/en/manual/checkout-settings/eu-withdrawal-function"
    assert by_url[both].regulation_tags == ("eudr", "withdrawal")

    # unit-level: two Hits for the same normalised URL with different tags merge
    a = Hit(EUR_LEX_2673 + "/?utm_source=x#frag", "t", None, "eur-lex.europa.eu", ("withdrawal",))
    b = Hit(EUR_LEX_2673, "t", "2023-11-22", "eur-lex.europa.eu", ("eudr",))
    merged = dedupe_hits([a, b])
    assert len(merged) == 1
    assert merged[0].url == EUR_LEX_2673
    assert merged[0].regulation_tags == ("eudr", "withdrawal")
    assert merged[0].published_date == "2023-11-22"


def test_search_all_covers_every_tag_domain_pair(fake_backend: RecordingBackend) -> None:
    ledger = CreditLedger()
    hits = search_all(fake_backend, ledger=ledger)
    pairs = {(kw["query"], kw["include_domains"][0]) for kw in fake_backend.search_calls}
    expected = {
        (r["query"], d)
        for r in SEED_REGULATIONS
        for d, tags in SOURCE_DOMAINS.items()
        if r["tag"] in tags
    }
    assert len(fake_backend.search_calls) == 14
    assert pairs == expected
    assert ledger.n_search_calls == 14
    assert ledger.credits == 28
    assert ledger.n_extract_calls == 0
    assert len(hits) == 16


def test_ledger_falls_back_when_usage_absent() -> None:
    ledger = CreditLedger()
    assert ledger.add_search({"results": []}) == 2
    assert ledger.add_search({"results": [], "usage": {"credits": 5}}) == 5
    assert ledger.add_extract({"results": [], "failed_results": []}, 16) == 2 * math.ceil(16 / 5)
    assert ledger.add_extract({"results": [], "usage": {"credits": 3}}, 1) == 3
    assert ledger.credits == 2 + 5 + 8 + 3
    assert ledger.n_search_calls == 2
    assert ledger.n_extract_calls == 2


def test_live_backend_refuses_empty_key() -> None:
    with pytest.raises(RuntimeError):
        LiveTavilyBackend("")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://apps.shopify.com/revoka?surface_detail=eu-withdrawal", None),
        ("https://cdn.shopify.com/s/files/1/x.pdf", None),
        ("https://community.shopify.com/t/foo/1", None),
        ("https://www.shopify.com/ph/blog/micro-business", None),
        ("https://www.shopify.com/blog/eu-withdrawal-button", "shopify.com"),
        (
            "https://help.shopify.com/en/manual/compliance/legal/eu-right-of-withdrawal",
            "help.shopify.com",
        ),
        ("https://www.ihk.de/pfalz/recht/x", "ihk.de"),
        ("https://www.wuerzburg.ihk.de/news", "ihk.de"),
        ("https://hdr4.bmj.de/a.html", "bmj.de"),
        ("https://eur-lex.europa.eu/eli/dir/2023/2673/oj", "eur-lex.europa.eu"),
    ],
)
def test_allowed_domain_table(url: str, expected: str | None) -> None:
    assert allowed_domain(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://help.shopify.com/fr/manual/compliance/legal/eu-right-of-withdrawal", "fr"),
        ("https://help.shopify.com/zh-TW/manual/compliance/legal/eu-right-of-withdrawal", "zh"),
        ("https://help.shopify.com/de/manual/compliance/legal/eu-right-of-withdrawal", None),
        ("https://help.shopify.com/en/manual/compliance/legal/eu-right-of-withdrawal", None),
        ("https://commission.europa.eu/law/timeshare-directive_es", "es"),
        ("https://commission.europa.eu/law/timeshare-directive_en", None),
        ("https://commission.europa.eu/law/timeshare-directive_en?prefLang=fi", "fi"),
        ("https://eur-lex.europa.eu/legal-content/sl/TXT/?uri=CELEX:32023L2673", "sl"),
        ("https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32023L2673", None),
        ("https://eur-lex.europa.eu/EN/legal-content/summary/withdrawal.html", None),
        ("https://green-forum.ec.europa.eu/document/download/x/eudr_da", "da"),
        ("https://www.shopify.com/ph/blog/x", None),
        ("https://www.ihk.de/x/laenderinformationen/eu/eudr-pflichten", None),
        ("https://www.gesetze-im-internet.de/englisch_wregg/englisch_wregg.html", None),
        ("https://www.bmuv.de/en/residential-wood-heating", None),
        ("https://www.bmj.de/x/0903_EmpCo.html", None),
        ("https://eur-lex.europa.eu/page?Locale=PL", "pl"),
        ("https://eur-lex.europa.eu/page?lang=en", None),
        ("https://eur-lex.europa.eu/eli/dir/2023/2673/oj", None),
        ("https://eur-lex.europa.eu/eli/reg/2023/1115/oj", None),
        ("https://www.ihk.de/it-recht/widerruf", None),
        ("https://www.ihk.de/pfalz/recht/faq_it-sicherheit.pdf", None),
        ("https://www.ihk.de/x/y_et-al", None),
    ],
)
def test_foreign_language_table(url: str, expected: str | None) -> None:
    assert foreign_language(url) == expected


class _MixedLangBackend:
    def search(self, **kw: object) -> dict[str, object]:
        return {
            "results": [
                {"url": "https://help.shopify.com/fr/manual/x", "title": "fr"},
                {"url": "https://help.shopify.com/en/manual/x", "title": "en"},
            ],
            "usage": {"credits": 2},
        }

    def extract(self, **kw: object) -> dict[str, object]:
        raise AssertionError("not used")


def test_search_drops_foreign_language_hits(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="regwatch.ingest.search"):
        hits = search_regulation(
            _MixedLangBackend(),
            "withdrawal",
            "q",
            ledger=CreditLedger(),  # type: ignore[arg-type]
        )
    assert {h.url for h in hits} == {"https://help.shopify.com/en/manual/x"}
    assert any("dropped foreign-language hit" in r.getMessage() for r in caplog.records)
