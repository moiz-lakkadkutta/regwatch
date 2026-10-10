import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from regwatch.config import EU_COUNTRIES
from regwatch.db import current_profile, session
from regwatch.schemas import Profile
from tests.conftest import ART_11A, SEED_CHANGES, seed_change_id

SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib

    import regwatch.app as appmod

    importlib.reload(appmod)
    from regwatch.jobs.crawl import seed

    assert seed() == SEED_CHANGES
    assert seed() == 0  # idempotent
    return TestClient(appmod.app)


@pytest.fixture()
def empty_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    import importlib

    import regwatch.app as appmod

    importlib.reload(appmod)
    return TestClient(appmod.app)


def _demo_profile() -> dict[str, Any]:
    demo: dict[str, Any] = json.loads((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    return demo


def _stored_profile(client: TestClient) -> Profile:
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        row = current_profile(s)
    assert row is not None
    return row.to_schema()


def test_feed_shows_both_seed_changes(client: TestClient) -> None:
    r = client.get("/feed")
    assert r.status_code == 200
    assert "Directive (EU) 2023/2673" in r.text and "2023/1115" in r.text
    assert "Applies to you" in r.text and "credits used" in r.text


def test_month_view_marks_overdue_and_toggles(client: TestClient) -> None:
    r = client.get("/month")
    # spelled-out date in a calm, factual label (docs/ux-brief.md §3.3)
    assert 'in force since <time datetime="2026-06-19">19 June 2026</time>' in r.text
    key = r.text.split('action="/todo/')[1].split("/")[0]
    r2 = client.post(f"/todo/{key}/toggle", follow_redirects=True)
    assert 'class="todo done"' in r2.text


def test_change_detail_has_quotes_and_source(client: TestClient) -> None:
    r = client.get(f"/change/{seed_change_id(ART_11A)}")
    assert r.status_code == 200
    assert "withdrawal function" in r.text and "eur-lex.europa.eu" in r.text


def test_profile_redirect_when_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'e.db'}")
    import importlib

    import regwatch.app as appmod

    importlib.reload(appmod)
    c = TestClient(appmod.app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/profile"
    os.environ.pop("REGWATCH_DB_URL", None)


def test_post_profile_stores_demo_profile(empty_client: TestClient) -> None:
    demo = _demo_profile()
    r = empty_client.post("/profile", data=demo, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/feed"
    assert _stored_profile(empty_client) == Profile(**demo)


def test_get_profile_renders_saved_profile_back(empty_client: TestClient) -> None:
    demo = _demo_profile()
    demo["platform"] = "woocommerce"
    empty_client.post("/profile", data=demo, follow_redirects=False)
    r = empty_client.get("/profile")
    assert r.status_code == 200
    assert '<option value="DE" selected>' in r.text
    for c in demo["ship_to"]:
        assert f'<option value="{c}" selected>' in r.text
    for key in demo["categories"]:
        assert f'<option value="{key}" selected>' in r.text
    assert 'value="woocommerce" selected' in r.text
    assert 'value="shopify" selected' not in r.text


def test_all_eu_expands_to_every_eu_country(empty_client: TestClient) -> None:
    empty_client.post(
        "/profile",
        data={"establishment_country": "DE", "ship_to": ["ALL_EU"], "categories": ["leather"]},
        follow_redirects=False,
    )
    stored = _stored_profile(empty_client)
    assert stored.ship_to == sorted(EU_COUNTRIES)
    assert "ALL_EU" not in stored.ship_to


def test_profile_normalises_country_case_and_dedupes_lists(empty_client: TestClient) -> None:
    empty_client.post(
        "/profile",
        data={
            "establishment_country": "de",
            "ship_to": ["FR", "AT", "FR"],
            "categories": ["leather", "leather"],
        },
        follow_redirects=False,
    )
    stored = _stored_profile(empty_client)
    assert stored.establishment_country == "DE"
    assert stored.ship_to == ["AT", "FR"]
    assert stored.categories == ["leather"]


def test_profile_form_renders_when_empty(empty_client: TestClient) -> None:
    r = empty_client.get("/profile")
    assert r.status_code == 200
    assert "selected" not in r.text
