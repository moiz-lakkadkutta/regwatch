import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from regwatch.config import CATEGORIES, EU_COUNTRIES, PLATFORMS, SOURCE_DOMAINS
from regwatch.schemas import Applicability, Profile, RegChange

SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"
CHANGE_FILES = sorted(SEED_DIR.glob("change-*.json"))


def test_demo_profile_matches_config() -> None:
    profile = Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    assert profile.establishment_country in EU_COUNTRIES
    assert set(profile.ship_to) <= set(EU_COUNTRIES)
    assert set(profile.categories) <= {k for k, _ in CATEGORIES}
    assert profile.platform in {k for k, _ in PLATFORMS}


@pytest.mark.parametrize("path", CHANGE_FILES, ids=[p.name for p in CHANGE_FILES])
def test_seed_changes_are_grounded(path: Path) -> None:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    change = RegChange.model_validate(payload["change"])
    appl = Applicability.model_validate(payload["applicability"])
    assert appl.ungrounded_todos(change) == []
    assert all(o.quote.strip() for o in change.obligations)
    assert all(t.citation_url == change.document_url for t in appl.todos)
    assert urlparse(change.document_url).hostname in SOURCE_DOMAINS
    assert all(t.due is not None for t in appl.todos)
