import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib

    import regwatch.app as appmod

    importlib.reload(appmod)
    from regwatch.jobs.crawl import seed

    assert seed() == 2
    assert seed() == 0  # idempotent
    return TestClient(appmod.app)


def test_feed_shows_both_seed_changes(client: TestClient) -> None:
    r = client.get("/feed")
    assert r.status_code == 200
    assert "Directive (EU) 2023/2673" in r.text and "2023/1115" in r.text
    assert "Applies to you" in r.text and "credits used" in r.text


def test_month_view_marks_overdue_and_toggles(client: TestClient) -> None:
    r = client.get("/month")
    assert "in force since 2026-06-19" in r.text
    key = r.text.split('action="/todo/')[1].split("/")[0]
    r2 = client.post(f"/todo/{key}/toggle", follow_redirects=True)
    assert 'class="todo done"' in r2.text


def test_change_detail_has_quotes_and_source(client: TestClient) -> None:
    r = client.get("/change/1")
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
