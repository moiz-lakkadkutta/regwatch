"""Example stores (regwatch.presets): seed loading, the switcher cookie, classify-presets."""

from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import col, select
from typer.testing import CliRunner

from regwatch.config import Settings
from regwatch.db import ApplicabilityRow, ProfileRow, TodoStateRow, make_engine, session
from regwatch.presets import append_entry, load_seed_changes, load_stores, read_entries
from regwatch.schemas import Applicability, Todo
from tests.conftest import ART_11A, EUDR_UNCLEAR, SEED_CHANGES, SEED_DIR, seed_file, seed_payload
from tests.fakes import ScriptedLLM

STORES = load_stores(SEED_DIR)
DEFAULT = STORES[0]
OTHERS = STORES[1:]


# ---------- the committed stores ----------


def test_committed_stores_are_valid() -> None:
    assert DEFAULT.file.default and DEFAULT.slug == "de-furniture"
    assert len(STORES) >= 4
    assert len({st.file.profile.model_dump_json() for st in STORES}) == len(STORES)
    changes = {sc.key: sc.change for sc in load_seed_changes(SEED_DIR)}
    assert not DEFAULT.appl_path.exists()  # the default store's results are the change files
    for st in OTHERS:
        entries = read_entries(st.appl_path)
        assert entries, f"{st.slug}: run `regwatch classify-presets`"
        for key, entry in entries.items():
            assert key in changes, f"{st.slug}: {key} has no change file"
            assert entry.applicability.ungrounded_todos(changes[key]) == []
            assert all(t.citation_url == key[0] for t in entry.applicability.todos)


# ---------- seed ----------


def _settings(tmp_path: Path, seed_dir: Path = SEED_DIR, db: str = "p.db") -> Settings:
    return Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / db}", REGWATCH_SEED_DIR=seed_dir)


def _appl_counts(settings: Settings) -> dict[str, int]:
    with session(make_engine(settings)) as s:
        out: dict[str, int] = {}
        for p in s.exec(select(ProfileRow).order_by(col(ProfileRow.id))).all():
            rows = s.exec(select(ApplicabilityRow).where(ApplicabilityRow.profile_id == p.id))
            out[p.slug or "-"] = len(rows.all())
        return out


def test_seed_loads_every_store_idempotently(tmp_path: Path) -> None:
    from regwatch.jobs.crawl import seed

    settings = _settings(tmp_path)
    assert seed(settings) == SEED_CHANGES
    with session(make_engine(settings)) as s:
        rows = s.exec(select(ProfileRow).order_by(col(ProfileRow.id))).all()
    assert [r.slug for r in rows] == [st.slug for st in STORES]
    assert rows[0].id == 1 and rows[0].to_schema() == DEFAULT.file.profile
    assert all(r.name and r.blurb for r in rows)
    counts = _appl_counts(settings)
    assert counts[DEFAULT.slug] == SEED_CHANGES
    for st in OTHERS:
        assert counts[st.slug] == len(read_entries(st.appl_path))
    assert seed(settings) == 0
    assert _appl_counts(settings) == counts


@pytest.fixture()
def mini_seed(tmp_path: Path) -> Path:
    """Two seed changes, the default store and one example store `xx-test` with no results."""
    d = tmp_path / "seed"
    (d / "stores").mkdir(parents=True)
    for name in ("profile.json", "documents.jsonl"):
        shutil.copy(SEED_DIR / name, d / name)
    for inst in (ART_11A, EUDR_UNCLEAR):
        shutil.copy(seed_file(inst), d / seed_file(inst).name)
    shutil.copy(DEFAULT.path, d / "stores" / DEFAULT.path.name)
    store = {
        "slug": "xx-test",
        "name": "Test shop",
        "blurb": "Sells rubber to Spain.",
        "profile": {
            "establishment_country": "ES",
            "ship_to": ["ES"],
            "categories": ["rubber"],
            "platform": "other",
            "company_size": None,
            "supply": None,
        },
    }
    (d / "stores" / "xx-test.json").write_text(json.dumps(store), encoding="utf-8")
    return d


def _payload(inst: str) -> dict[str, Any]:
    return seed_payload(seed_file(inst))


def _grounded(inst: str, decision: str = "applies") -> Applicability:
    change = load_change(inst)
    todos = (
        [
            Todo(
                text="Do the first obligation",
                due=None,
                quote=change.obligations[0].quote,
                citation_url=change.document_url,
            )
        ]
        if decision != "does_not"
        else []
    )
    return Applicability.model_validate(
        {"decision": decision, "reasons": ["Because."], "todos": todos}
    )


def load_change(inst: str) -> Any:
    from regwatch.schemas import RegChange

    return RegChange.model_validate(_payload(inst)["change"])


def test_seed_fills_store_gaps_on_rerun(tmp_path: Path, mini_seed: Path) -> None:
    from regwatch.jobs.crawl import seed

    settings = _settings(tmp_path, mini_seed)
    assert seed(settings) == 2
    assert _appl_counts(settings) == {"de-furniture": 2, "xx-test": 0}
    jsonl = mini_seed / "stores" / "xx-test.applicability.jsonl"
    append_entry(jsonl, load_change(ART_11A), _grounded(ART_11A), model="m", cost_usd=0.01)
    assert seed(settings) == 0
    assert _appl_counts(settings) == {"de-furniture": 2, "xx-test": 1}
    append_entry(
        jsonl, load_change(EUDR_UNCLEAR), _grounded(EUDR_UNCLEAR, "unclear"), model="m", cost_usd=0
    )
    seed(settings)
    seed(settings)
    assert _appl_counts(settings) == {"de-furniture": 2, "xx-test": 2}


def test_seed_refuses_an_ungrounded_store_line(tmp_path: Path, mini_seed: Path) -> None:
    from regwatch.jobs.crawl import seed

    change = load_change(ART_11A)
    line = {
        "document_url": change.document_url,
        "instrument": change.instrument,
        "model": "m",
        "cost_usd": 0.0,
        "applicability": {
            "decision": "applies",
            "reasons": ["x"],
            "todos": [{"text": "Invent", "due": None, "quote": "not a quote", "citation_url": "u"}],
        },
    }
    (mini_seed / "stores" / "xx-test.applicability.jsonl").write_text(json.dumps(line) + "\n")
    with pytest.raises(ValueError, match="not grounded"):
        seed(_settings(tmp_path, mini_seed))


def test_default_store_must_match_profile_json(mini_seed: Path) -> None:
    p = mini_seed / "stores" / "de-furniture.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["profile"]["company_size"] = "larger"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="profile.json"):
        load_stores(mini_seed)


def test_seed_adopts_the_demo_profile_of_an_older_db(tmp_path: Path) -> None:
    """A DB made before stores: profilerow lacks slug/name/blurb and holds the demo profile."""
    db = tmp_path / "old.db"
    demo = json.loads((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE profilerow (id INTEGER PRIMARY KEY, establishment_country VARCHAR NOT NULL,"
        " ship_to VARCHAR NOT NULL, categories VARCHAR NOT NULL, platform VARCHAR NOT NULL,"
        " company_size VARCHAR, supply VARCHAR, created_at DATETIME NOT NULL)"
    )
    con.execute(
        "INSERT INTO profilerow VALUES (1, ?, ?, ?, ?, ?, NULL, '2026-10-01 00:00:00')",
        (
            demo["establishment_country"],
            json.dumps(demo["ship_to"]),
            json.dumps(demo["categories"]),
            demo["platform"],
            demo["company_size"],
        ),
    )
    con.commit()
    con.close()
    from regwatch.jobs.crawl import seed

    settings = Settings(REGWATCH_DB_URL=f"sqlite:///{db}", REGWATCH_SEED_DIR=SEED_DIR)
    seed(settings)
    with session(make_engine(settings)) as s:
        rows = s.exec(select(ProfileRow).order_by(col(ProfileRow.id))).all()
    assert [r.slug for r in rows] == [st.slug for st in STORES]  # row 1 adopted, no duplicate
    assert rows[0].id == 1


# ---------- the switcher ----------


@pytest.fixture()
def store_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'sw.db'}")
    monkeypatch.setenv("REGWATCH_PROFILE_LOCKED", "true")
    import regwatch.app as appmod

    importlib.reload(appmod)
    from regwatch.jobs.crawl import seed

    seed()
    return TestClient(appmod.app)


NL = next(st for st in STORES if st.slug == "nl-clothing")


def _current(html: str) -> str:
    return html.split('aria-current="true">', 1)[1].split("<", 1)[0]


@pytest.mark.parametrize("page", ["/feed", "/month", "/profile"])
def test_switcher_sets_the_cookie_and_returns_to_the_page(
    store_client: TestClient, page: str
) -> None:
    html = store_client.get(page).text
    assert 'aria-label="Example stores"' in html and _current(html) == DEFAULT.file.name
    for st in STORES:
        assert f'href="/store/{st.slug}?next={page}"' in html
    r = store_client.get(f"/store/{NL.slug}?next={page}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == page
    cookie = r.headers["set-cookie"].lower()
    assert "rw_store=nl-clothing" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    html = store_client.get(page).text
    assert _current(html) == NL.file.name and NL.file.blurb in html


def test_feed_month_profile_follow_the_chosen_store(store_client: TestClient) -> None:
    assert "Established in Germany" in store_client.get("/feed").text
    store_client.get(f"/store/{NL.slug}")
    assert "Established in Netherlands" in store_client.get("/feed").text
    profile = store_client.get("/profile").text
    assert f"Example store: {NL.file.name}" in profile and "can't be changed here" in profile
    assert '<option value="NL" selected>' in profile and 'type="submit"' not in profile
    month = store_client.get("/month").text
    assert "What to do this month" in month


def test_unknown_store_falls_back_to_the_default(store_client: TestClient) -> None:
    store_client.cookies.set("rw_store", "no-such-store")
    html = store_client.get("/feed").text
    assert "Established in Germany" in html and _current(html) == DEFAULT.file.name
    store_client.get(f"/store/{NL.slug}")
    r = store_client.get("/store/no-such-store?next=https://evil.example", follow_redirects=False)
    assert r.headers["location"] == "/feed" and 'rw_store=""' in r.headers["set-cookie"]


def test_todo_ticks_belong_to_the_chosen_store(store_client: TestClient) -> None:
    import regwatch.app as appmod

    store_client.get(f"/store/{NL.slug}")
    html = store_client.get("/month").text
    key = html.split('action="/todo/')[1].split("/")[0]
    store_client.post(f"/todo/{key}/toggle")
    with session(appmod.engine) as s:
        (tick,) = s.exec(select(TodoStateRow)).all()
        nl = s.exec(select(ProfileRow).where(ProfileRow.slug == NL.slug)).one()
    assert tick.profile_id == nl.id and tick.todo_key == key
    store_client.cookies.clear()
    assert 'class="todo done"' not in store_client.get("/month").text


def test_unlocked_saved_profile_is_its_own_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'open.db'}")
    import regwatch.app as appmod

    importlib.reload(appmod)
    from regwatch.jobs.crawl import seed

    seed()
    client = TestClient(appmod.app)
    client.get(f"/store/{NL.slug}")
    assert "Filled in from the example store" in client.get("/profile").text
    r = client.post(
        "/profile",
        data={"establishment_country": "FR", "ship_to": ["FR"], "categories": ["leather"]},
        follow_redirects=False,
    )
    assert r.status_code == 303 and 'rw_store=""' in r.headers["set-cookie"]
    html = client.get("/feed").text
    assert "Established in France" in html and _current(html) == "Your store"
    client.get(f"/store/{NL.slug}")
    assert "Established in Netherlands" in client.get("/feed").text
    client.get("/store/mine")
    assert "Established in France" in client.get("/feed").text


# ---------- classify-presets ----------


def _reply(appl: Applicability) -> str:
    return json.dumps(
        {
            "decision": appl.decision,
            "reasons": appl.reasons,
            "todos": [{"text": t.text, "due": None, "quote": t.quote} for t in appl.todos],
        }
    )


def test_classify_presets_fills_only_gaps(tmp_path: Path, mini_seed: Path) -> None:
    from regwatch.jobs.presets import classify_presets

    settings = _settings(tmp_path, mini_seed)
    llm = ScriptedLLM([_reply(_grounded(ART_11A)), _reply(_grounded(EUDR_UNCLEAR, "unclear"))])
    summary = classify_presets(settings, backend=llm)
    (tally,) = summary.stores
    assert (tally.slug, tally.n_applies, tally.n_unclear, tally.n_existing) == ("xx-test", 1, 1, 0)
    assert summary.n_calls == 2 and summary.cost_usd > 0
    assert len(llm.calls) == 2 and "Spain (ES)" in llm.calls[0][1]["content"]
    jsonl = mini_seed / "stores" / "xx-test.applicability.jsonl"
    entries = read_entries(jsonl)
    assert [e.applicability.decision for e in entries.values()] == ["applies", "unclear"]
    assert not (mini_seed / "stores" / "de-furniture.applicability.jsonl").exists()
    # a rerun calls nothing; a new change file is the only gap filled
    again = classify_presets(settings, backend=ScriptedLLM([]))
    assert again.n_calls == 0 and again.stores[0].n_existing == 2
    eudr_more = seed_file("Regulation (EU) 2023/1115 — Article 9")
    shutil.copy(eudr_more, mini_seed / eudr_more.name)
    llm3 = ScriptedLLM([json.dumps({"decision": "does_not", "reasons": ["No."], "todos": []})])
    third = classify_presets(settings, backend=llm3)
    assert third.stores[0].n_does_not == 1 and third.stores[0].n_existing == 2
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 3


def test_classify_presets_leaves_failures_for_the_next_run(tmp_path: Path, mini_seed: Path) -> None:
    from regwatch.jobs.presets import classify_presets

    settings = _settings(tmp_path, mini_seed)
    summary = classify_presets(settings, backend=ScriptedLLM(["not json", "still not json"]))
    assert summary.stores[0].n_failed == 2  # the second change finds no reply left
    assert not (mini_seed / "stores" / "xx-test.applicability.jsonl").exists()


def test_classify_presets_refuses_ungrounded(
    tmp_path: Path, mini_seed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import regwatch.jobs.presets as jobs
    from regwatch.understand.classify import ClassifyResult

    bad = Applicability(
        decision="applies",
        reasons=["x"],
        todos=[Todo(text="Invent", due=None, quote="nowhere in the law", citation_url="u")],
    )

    def fake_classify(*_: Any, **__: Any) -> ClassifyResult:
        return ClassifyResult(bad, False, [], 0, None, 0.0, "m", None)

    monkeypatch.setattr(jobs, "classify", fake_classify)
    summary = jobs.classify_presets(_settings(tmp_path, mini_seed), backend=ScriptedLLM([]))
    assert summary.stores[0].n_refused == 2
    assert not (mini_seed / "stores" / "xx-test.applicability.jsonl").exists()
    with pytest.raises(ValueError, match="not grounded"):
        append_entry(tmp_path / "x.jsonl", load_change(ART_11A), bad, model="m", cost_usd=0)
    assert not (tmp_path / "x.jsonl").exists()


def test_classify_presets_cli_with_the_fixtures_backend(
    tmp_path: Path, mini_seed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from regwatch.cli import app

    fixtures = tmp_path / "llm"
    (fixtures / "classify").mkdir(parents=True)
    for i, inst in enumerate((ART_11A, EUDR_UNCLEAR)):
        rec = {
            "url": load_change(inst).document_url,
            "responses": [
                {
                    "text": _reply(_grounded(inst)),
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
                }
            ],
        }
        (fixtures / "classify" / f"{i}.json").write_text(json.dumps(rec), encoding="utf-8")
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    args = ["classify-presets", "--seed-dir", str(mini_seed), "--llm-backend", "fixtures"]
    args += ["--llm-fixtures-dir", str(fixtures)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "xx-test: 2 added (2 apply" in result.output
    assert "2 model calls · 2000/200 tokens" in result.output
    assert len(read_entries(mini_seed / "stores" / "xx-test.applicability.jsonl")) == 2
    bad = CliRunner().invoke(app, [*args, "--store", "nope"])
    assert bad.exit_code == 1 and "unknown store" in bad.output
