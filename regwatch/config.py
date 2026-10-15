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
    # USD per million tokens for the extractor model — Token Factory catalog price, checked 2026-10-10.
    extractor_price_in_per_mtok: float = Field(
        default=0.06, alias="REGWATCH_EXTRACTOR_PRICE_IN_PER_MTOK"
    )
    extractor_price_out_per_mtok: float = Field(
        default=0.24, alias="REGWATCH_EXTRACTOR_PRICE_OUT_PER_MTOK"
    )
    # Characters of markdown sent to the extractor; quotes are still validated on the full text.
    # 200k keeps late articles of long acts (2023/2673 Art. 11a sits at ~66k of ~106k chars).
    normalize_max_chars: int = Field(default=200_000, alias="REGWATCH_NORMALIZE_MAX_CHARS")
    # EUR-Lex acts longer than this are split per article / amendment point before normalising.
    act_split_min_chars: int = Field(default=20_000, alias="REGWATCH_ACT_SPLIT_MIN_CHARS")
    normalize_concurrency: int = Field(default=4, alias="REGWATCH_NORMALIZE_CONCURRENCY")
    # Lightning reasons by default and writes the trace into `content`, exhausting max_tokens
    # before any JSON; off unless set (sent as chat_template_kwargs.enable_thinking).
    extractor_thinking: bool = Field(default=False, alias="REGWATCH_EXTRACTOR_THINKING")
    # USD per million tokens for the classifier model — Token Factory catalog price, checked 2026-10-10.
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
    # "Let the agent check" (docs/plans/agent-unclear-spec.md): off unless set; caps are hard.
    agent_enabled: bool = Field(default=False, alias="REGWATCH_AGENT_ENABLED")
    agent_daily_usd: float = Field(default=1.0, alias="REGWATCH_AGENT_DAILY_USD")
    agent_daily_credits: int = Field(default=60, alias="REGWATCH_AGENT_DAILY_CREDITS")
    agent_run_steps: int = Field(default=6, alias="REGWATCH_AGENT_RUN_STEPS")
    agent_run_credits: int = Field(default=10, alias="REGWATCH_AGENT_RUN_CREDITS")
    agent_visitor_runs_per_hour: int = Field(
        default=5, alias="REGWATCH_AGENT_VISITOR_RUNS_PER_HOUR"
    )
    # The public demo shares one profile; locked, POST /profile is refused so nobody blanks it.
    profile_locked: bool = Field(default=False, alias="REGWATCH_PROFILE_LOCKED")


# Only these domains are ever searched or extracted. Official sources first, platform guidance last.
SOURCE_DOMAINS: dict[str, list[str]] = {
    "eur-lex.europa.eu": ["withdrawal", "eudr", "greenclaims", "packaging", "productsafety"],
    "commission.europa.eu": ["withdrawal", "eudr", "greenclaims", "packaging", "productsafety"],
    "green-forum.ec.europa.eu": ["eudr"],
    "gesetze-im-internet.de": ["withdrawal"],
    "bmj.de": ["withdrawal"],
    "bmuv.de": ["eudr"],
    "ihk.de": ["withdrawal", "eudr", "greenclaims", "packaging", "productsafety"],
    "help.shopify.com": ["withdrawal", "eudr", "productsafety"],
    "shopify.com": ["withdrawal", "eudr"],
}

# A key listed here only accepts URLs whose host + path starts with one of the prefixes.
SOURCE_URL_PREFIXES: dict[str, tuple[str, ...]] = {
    "shopify.com": ("www.shopify.com/blog/", "shopify.com/blog/"),
}

# Language copies: URLs marked with a code in LANG_CODES - KEEP_LANGS are dropped at search/extract.
KEEP_LANGS: frozenset[str] = frozenset({"en", "de"})
LANG_CODES: frozenset[str] = frozenset(
    "bg cs da de el en es et fi fr ga hr hu it lt lv mt nl pl pt ro sk sl sv ru ja ko zh tr th".split()
)

# Seed regulations: what the first crawl looks for. Dates are as known on 2026-09-14 — verify at build.
SECTION_SEP = " — "  # joins an act's instrument and a section label: "<act> — <label>"


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
        "query": "EUDR Regulation 2023/1115 as amended by Regulation 2025/2650 application date micro small enterprises due diligence statement wood leather furniture",
        # Micro and small operators; medium and large from 2026-12-30 (Regulation (EU) 2025/2650).
        "applies_from": "2027-06-30",
    },
    # Added 2026-10-15. `applies_from` below is NOT yet checked against crawled text (Tavily plan
    # exhausted, see FEEDBACK.md 2026-10-15); it is unused at runtime. To-do dates in the feed
    # come from grounded quotes only, never from here.
    {
        "tag": "greenclaims",
        "instrument": "Directive (EU) 2024/825 — empowering consumers for the green transition",
        "query": "Directive (EU) 2024/825 empowering consumers for the green transition generic environmental claims sustainability labels durability information apply from 27 September 2026",
        "applies_from": "2026-09-27",
    },
    {
        "tag": "packaging",
        "instrument": "Regulation (EU) 2025/40 — Packaging and Packaging Waste Regulation (PPWR)",
        "query": "Regulation (EU) 2025/40 packaging and packaging waste PPWR obligations distributors e-commerce empty space ratio labelling application 12 August 2026",
        "applies_from": "2026-08-12",
    },
    {
        "tag": "productsafety",
        "instrument": "Regulation (EU) 2023/988 — General Product Safety Regulation (GPSR)",
        "query": "Regulation (EU) 2023/988 General Product Safety Regulation GPSR distance sales online marketplace product information Article 19 applies from 13 December 2024",
        "applies_from": "2024-12-13",
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

# EUDR dates and duties depend on these two facts (Regulation (EU) 2025/2650). Keys == schemas.
COMPANY_SIZES: list[tuple[str, str]] = [
    ("small", "Fewer than 50 staff and turnover up to €10 million"),
    ("larger", "Bigger than that"),
]

SUPPLY_OPTIONS: list[tuple[str, str]] = [
    ("eu_suppliers", "I buy them from suppliers in the EU"),
    ("imports_or_makes", "I import them from outside the EU, or make them myself"),
]
REGULATION_TAGS: frozenset[str] = frozenset(r["tag"] for r in SEED_REGULATIONS)

# Plain-language group headings for the feed (what to do first; the instrument name is the subline).
REGULATION_LABELS: dict[str, str] = {
    "withdrawal": "Withdrawal button for online orders",
    "eudr": "Deforestation-free products (EUDR)",
    "greenclaims": "Green claims and durability info on product pages",
    "packaging": "Packaging rules for shipped orders (PPWR)",
    "productsafety": "Product safety info on every listing (GPSR)",
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
