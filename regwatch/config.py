"""Settings, the fixed source allow-list and the seed regulations."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    nebius_api_key: str = Field(default="", alias="NEBIUS_API_KEY")
    tokenfactory_base_url: str = Field(
        default="https://api.tokenfactory.nebius.com/v1", alias="TOKENFACTORY_BASE_URL"
    )
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    classifier_model: str = Field(
        default="nvidia/Nemotron-3-Ultra-550b-a55b", alias="REGWATCH_CLASSIFIER_MODEL"
    )
    extractor_model: str = Field(
        default="nvidia/Nemotron-3_5-Lightning", alias="REGWATCH_EXTRACTOR_MODEL"
    )
    db_url: str = Field(default="sqlite:///regwatch.db", alias="REGWATCH_DB_URL")


# Only these domains are ever searched or extracted. Official sources first, platform guidance last.
SOURCE_DOMAINS: dict[str, list[str]] = {
    "eur-lex.europa.eu": ["withdrawal", "eudr"],
    "commission.europa.eu": ["withdrawal", "eudr"],
    "green-forum.ec.europa.eu": ["eudr"],
    "gesetze-im-internet.de": ["withdrawal"],
    "bmj.de": ["withdrawal"],
    "bmuv.de": ["eudr"],
    "ihk.de": ["withdrawal", "eudr"],
    "help.shopify.com": ["withdrawal", "eudr"],
    "shopify.com": ["withdrawal", "eudr"],
}

# Seed regulations: what the first crawl looks for. Dates are as known on 2026-09-14 — verify at build.
SEED_REGULATIONS: list[dict[str, str]] = [
    {
        "tag": "withdrawal",
        "instrument": "Directive (EU) 2023/2673 — withdrawal function (DE: § 356a BGB)",
        "query": "Directive 2023/2673 withdrawal function button distance contracts 19 June 2026 § 356a BGB Widerrufsbutton",
        "applies_from": "2026-06-19",
    },
    {
        "tag": "eudr",
        "instrument": "Regulation (EU) 2023/1115 — Deforestation Regulation (EUDR)",
        "query": "EUDR Regulation 2023/1115 application date small micro enterprises due diligence statement wood leather furniture",
        "applies_from": "2026-06-30",
    },
]

CATEGORIES: list[tuple[str, str]] = [
    ("furniture_wood", "Furniture and wooden goods"),
    ("leather", "Leather goods"),
    ("coffee_cocoa", "Coffee, cocoa, chocolate"),
    ("paper_print", "Paper and printed products"),
    ("rubber", "Rubber products"),
    ("apparel", "Clothing and textiles"),
    ("electronics", "Electronics"),
    ("cosmetics", "Cosmetics"),
    ("other", "Other"),
]

EU_COUNTRIES: list[str] = [
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
]

PLATFORMS: list[tuple[str, str]] = [
    ("shopify", "Shopify"),
    ("woocommerce", "WooCommerce"),
    ("other", "Other"),
]
REGULATION_TAGS: frozenset[str] = frozenset(r["tag"] for r in SEED_REGULATIONS)


def sources_for(tag: str) -> list[str]:
    """Allow-listed domains to search for one regulation tag, in SOURCE_DOMAINS order."""
    return [d for d, tags in SOURCE_DOMAINS.items() if tag in tags]
