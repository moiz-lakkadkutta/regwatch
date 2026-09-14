from datetime import date

from regwatch.config import (
    CATEGORIES,
    EU_COUNTRIES,
    PLATFORMS,
    REGULATION_TAGS,
    SEED_REGULATIONS,
    SOURCE_DOMAINS,
    sources_for,
)


def test_every_source_tag_is_a_seed_regulation() -> None:
    assert set().union(*SOURCE_DOMAINS.values()) <= REGULATION_TAGS


def test_seed_regulations_have_iso_dates_and_unique_tags() -> None:
    tags = [r["tag"] for r in SEED_REGULATIONS]
    assert len(tags) == len(set(tags))
    for r in SEED_REGULATIONS:
        date.fromisoformat(r["applies_from"])
        assert r["instrument"]
        assert r["query"]


def test_sources_for_each_tag_nonempty_and_allow_listed() -> None:
    for tag in REGULATION_TAGS:
        domains = sources_for(tag)
        assert domains
        assert set(domains) <= set(SOURCE_DOMAINS)
    assert sources_for("nope") == []
    assert "eur-lex.europa.eu" in sources_for("withdrawal")


def test_category_and_platform_keys_unique() -> None:
    category_keys = [k for k, _ in CATEGORIES]
    platform_keys = [k for k, _ in PLATFORMS]
    assert len(category_keys) == len(set(category_keys))
    assert len(platform_keys) == len(set(platform_keys))
    assert "shopify" in platform_keys
    assert len(EU_COUNTRIES) == 27
    assert len(set(EU_COUNTRIES)) == 27
    assert all(len(c) == 2 and c.isalpha() and c.isupper() for c in EU_COUNTRIES)
