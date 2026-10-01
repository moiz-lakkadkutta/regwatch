"""Settings, the fixed source allow-list and the seed regulations."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The recorded seed run next to the package in a checkout / editable install. A non-editable
# install (or the container) points REGWATCH_SEED_DIR at the copied examples directory instead.
DEFAULT_SEED_DIR = Path(__file__).resolve().parents[1] / "examples" / "seed-run"


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
    # USD per million tokens for the extractor model. PLACEHOLDERS — confirm against Token Factory pricing.
    extractor_price_in_per_mtok: float = Field(
        default=0.20, alias="REGWATCH_EXTRACTOR_PRICE_IN_PER_MTOK"
    )
    extractor_price_out_per_mtok: float = Field(
        default=0.60, alias="REGWATCH_EXTRACTOR_PRICE_OUT_PER_MTOK"
    )
    # Characters of markdown sent to the extractor; quotes are still validated on the full text.
    normalize_max_chars: int = Field(default=60_000, alias="REGWATCH_NORMALIZE_MAX_CHARS")
    normalize_concurrency: int = Field(default=4, alias="REGWATCH_NORMALIZE_CONCURRENCY")
    # USD per million tokens for the classifier model. PLACEHOLDERS — confirm against Token Factory pricing.
    classifier_price_in_per_mtok: float = Field(
        default=1.00, alias="REGWATCH_CLASSIFIER_PRICE_IN_PER_MTOK"
    )
    classifier_price_out_per_mtok: float = Field(
        default=3.00, alias="REGWATCH_CLASSIFIER_PRICE_OUT_PER_MTOK"
    )
    # Recipient of the "Report a wrong call" mailto on the change page; empty leaves the To: blank.
    report_email: str = Field(default="", alias="REGWATCH_REPORT_EMAIL")
    # Directory with profile.json, change-*.json and documents.jsonl that `regwatch seed` loads.
    seed_dir: Path = Field(default=DEFAULT_SEED_DIR, alias="REGWATCH_SEED_DIR")


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

# A key listed here only accepts URLs whose host + path starts with one of the prefixes.
SOURCE_URL_PREFIXES: dict[str, tuple[str, ...]] = {
    "shopify.com": ("www.shopify.com/blog/", "shopify.com/blog/"),
}

# Language copies: URLs marked with a code in LANG_CODES - KEEP_LANGS are dropped at search/extract.
KEEP_LANGS: frozenset[str] = frozenset({"en", "de"})
LANG_CODES: frozenset[str] = frozenset(
    "bg cs da de el en es et fi fr ga hr hu it lt lv mt nl pl pt ro sk sl sv ru ja ko zh".split()
)

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

# Plain names for the prompt ("Germany (DE)"); keys == EU_COUNTRIES.
COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IE": "Ireland",
    "IT": "Italy",
    "LV": "Latvia",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "NL": "Netherlands",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
}

PLATFORMS: list[tuple[str, str]] = [
    ("shopify", "Shopify"),
    ("woocommerce", "WooCommerce"),
    ("other", "Other"),
]
REGULATION_TAGS: frozenset[str] = frozenset(r["tag"] for r in SEED_REGULATIONS)

# Plain-language group headings for the feed (what to do first; the instrument name is the subline).
REGULATION_LABELS: dict[str, str] = {
    "withdrawal": "Withdrawal button for online orders",
    "eudr": "Deforestation-free products (EUDR)",
}


def country_name(code: str) -> str:
    """Plain country name for a two-letter code; the code itself when unknown."""
    return COUNTRY_NAMES.get(code, code)


def regulation_instrument(tag: str) -> str:
    """Instrument name of the SEED_REGULATIONS entry with `tag`; KeyError when unknown."""
    for r in SEED_REGULATIONS:
        if r["tag"] == tag:
            return r["instrument"]
    raise KeyError(tag)


def sources_for(tag: str) -> list[str]:
    """Allow-listed domains to search for one regulation tag, in SOURCE_DOMAINS order."""
    return [d for d, tags in SOURCE_DOMAINS.items() if tag in tags]
