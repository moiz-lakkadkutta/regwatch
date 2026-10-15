import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from regwatch.config import CATEGORIES, EU_COUNTRIES, PLATFORMS, SOURCE_DOMAINS
from regwatch.ingest.tavily_backend import allowed_domain
from regwatch.schemas import Applicability, Profile, RegChange

SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"
CHANGE_FILES = sorted(SEED_DIR.glob("change-*.json"))


def test_seed_change_files_present() -> None:
    """Guard for the parametrised grounding test: it must never pass vacuously."""
    assert len(CHANGE_FILES) >= 2


def test_demo_profile_matches_config() -> None:
    profile = Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    assert profile.establishment_country in EU_COUNTRIES
    assert set(profile.ship_to) <= set(EU_COUNTRIES)
    assert set(profile.categories) <= {k for k, _ in CATEGORIES}
    assert profile.platform in {k for k, _ in PLATFORMS}
    # EUDR dates and duties depend on these (Regulation (EU) 2025/2650)
    assert profile.company_size == "small"


@pytest.mark.parametrize("path", CHANGE_FILES, ids=[p.name for p in CHANGE_FILES])
def test_seed_changes_are_grounded(path: Path) -> None:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    change = RegChange.model_validate(payload["change"])
    appl = Applicability.model_validate(payload["applicability"])
    assert appl.ungrounded_todos(change) == []
    assert all(o.quote.strip() for o in change.obligations)
    assert all(t.citation_url == change.document_url for t in appl.todos)
    # the ingest allow-list rule (exact host or subdomain, e.g. www.ihk.de → ihk.de)
    assert allowed_domain(change.document_url) in SOURCE_DOMAINS
    assert urlparse(change.document_url).scheme == "https"
    assert all(t.due is not None for t in appl.todos)


def test_seed_run_documents_are_seed_change_sources() -> None:
    """R1: every recorded document is the real source of a seed change (never a fixture body)."""
    assert len(CHANGE_FILES) >= 2
    sources = {
        json.loads(p.read_text(encoding="utf-8"))["change"]["document_url"] for p in CHANGE_FILES
    }
    lines = (SEED_DIR / "documents.jsonl").read_text(encoding="utf-8").splitlines()
    urls = [json.loads(line)["url"] for line in lines if line.strip()]
    assert urls and urls == sorted(urls)
    assert set(urls) == sources
