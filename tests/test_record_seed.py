"""`regwatch record-seed`: a grounded crawl in the DB → examples/seed-run style files."""

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session, col, select
from typer.testing import CliRunner

from regwatch.cli import app
from regwatch.config import Settings
from regwatch.db import (
    ApplicabilityRow,
    CrawlRunRow,
    DocumentRow,
    ProfileRow,
    RegChangeRow,
    make_engine,
)
from regwatch.jobs.crawl import seed
from regwatch.jobs.record import RecordError, record_seed

ACT = "https://eur-lex.europa.eu/eli/dir/2023/2673/oj/eng"
SHOP = "https://help.shopify.com/en/manual/compliance/legal/eu-right-of-withdrawal"
ONLY_SKIPPED = "https://www.ihk.de/pfalz/recht/widerrufsbutton"
NOT_CHOSEN = "https://eur-lex.europa.eu/eli/reg/2023/1115/oj"

ACT_MD = (
    "# Directive (EU) 2023/2673\n\nThe trader shall provide a withdrawal function.\n\n"
    "Member States shall apply those measures from 19 June 2026.\n"
)
SHOP_MD = "# EU right of withdrawal\n\nYou must add a withdrawal button to your store.\n"
IHK_MD = "# Widerrufsbutton\n\nDer Button kommt.\n"


def _doc(s: Session, url: str, markdown: str, domain: str, tags: list[str]) -> None:
    s.add(
        DocumentRow(
            url=url,
            content_hash=f"hash-{len(markdown)}",
            source_domain=domain,
            published_date="2026-01-01",
            fetched_at=datetime(2026, 10, 1, 8, 0),
            markdown=markdown,
            regulation_tags=json.dumps(tags),
        )
    )


def _change(
    s: Session,
    url: str,
    instrument: str,
    quote: str,
    *,
    content_hash: str | None = None,
    day: int = 1,
) -> int:
    doc = s.get(DocumentRow, url)
    assert doc is not None
    row = RegChangeRow(
        document_url=url,
        content_hash=content_hash or doc.content_hash,
        model="nvidia/Nemotron-3_5-Lightning",
        instrument=instrument,
        jurisdiction="EU",
        summary=f"{instrument} summary.",
        effective_dates=json.dumps(["2026-06-19"]),
        obligations=json.dumps([{"text": "Do it.", "quote": quote}]),
        created_at=datetime(2026, 10, day, 9, 0),
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    assert row.id is not None
    return row.id


def _appl(
    s: Session,
    profile_id: int,
    change_id: int,
    decision: str,
    todos: list[dict[str, object]],
) -> None:
    s.add(
        ApplicabilityRow(
            profile_id=profile_id,
            regchange_id=change_id,
            decision=decision,
            reasons=json.dumps(["You ship to DE."]),
            todos=json.dumps(todos),
            model="nvidia/Nemotron-3-Ultra-550b-a55b",
            cost_usd=0.0123,
            tavily_credits=0,
        )
    )
    s.commit()


def _todo(url: str, quote: str, due: str | None = "2026-06-19") -> dict[str, object]:
    return {"text": "Add the button", "due": due, "quote": quote, "citation_url": url}


ACT_Q = "The trader shall provide a withdrawal function."
ACT_Q2 = "Member States shall apply those measures from 19 June 2026."
SHOP_Q = "You must add a withdrawal button to your store."


@pytest.fixture()
def crawl_db(tmp_path: Path) -> Settings:
    """A small grounded crawl: SHOP's change is stored before ACT's, so order is by url, not id."""
    settings = Settings(
        REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'crawl.db'}",
        REGWATCH_SEED_DIR=str(tmp_path / "seed-run"),
    )
    with Session(make_engine(settings)) as s:
        old = ProfileRow(establishment_country="FR", ship_to='["FR"]', categories='["leather"]')
        s.add(old)
        prof = ProfileRow(
            establishment_country="DE",
            ship_to='["AT", "DE"]',
            categories='["furniture_wood"]',
            platform="shopify",
        )
        s.add(prof)
        s.commit()
        s.refresh(old)
        s.refresh(prof)
        assert prof.id is not None and old.id is not None
        _doc(s, ACT, ACT_MD, "eur-lex.europa.eu", ["withdrawal"])
        _doc(s, SHOP, SHOP_MD, "help.shopify.com", [])
        _doc(s, ONLY_SKIPPED, IHK_MD, "ihk.de", ["withdrawal"])
        _doc(s, NOT_CHOSEN, "# EUDR\n\nOperators shall.\n", "eur-lex.europa.eu", ["eudr"])
        s.commit()
        shop = _change(s, SHOP, "EU Directive 2023/2673", SHOP_Q, day=3)
        _appl(s, prof.id, shop, "applies", [_todo(SHOP, SHOP_Q)])
        a1 = _change(s, ACT, "Directive (EU) 2023/2673 — Article 1, point (3)", ACT_Q, day=2)
        _appl(s, old.id, a1, "does_not", [])  # another profile's verdict is ignored
        _appl(s, prof.id, a1, "applies", [_todo(ACT, ACT_Q)])
        a2 = _change(s, ACT, "Directive (EU) 2023/2673 — Article 1, point (1)", ACT_Q2)
        _appl(s, prof.id, a2, "does_not", [])
        a3 = _change(s, ACT, "Directive (EU) 2023/2673 — ANNEX I", ACT_Q2)
        _appl(s, prof.id, a3, "unclear", [_todo(ACT, ACT_Q2, due=None)])
        _change(s, ACT, "Directive (EU) 2023/2673 — Article 2", ACT_Q2)  # never classified
        stale = _change(s, ACT, "Directive (EU) 2023/2673 — old", ACT_Q, content_hash="hash-old")
        _appl(s, prof.id, stale, "applies", [_todo(ACT, ACT_Q)])
        ihk = _change(s, ONLY_SKIPPED, "§ 356a BGB — IHK Pfalz guidance", "Der Button kommt.")
        _appl(s, prof.id, ihk, "unclear", [_todo(ONLY_SKIPPED, "Der Button kommt.", due=None)])
        nc = _change(s, NOT_CHOSEN, "Regulation (EU) 2023/1115", "Operators shall.")
        _appl(s, prof.id, nc, "applies", [_todo(NOT_CHOSEN, "Operators shall.")])
        # the source crawl: the latest stage "all" run (40 credits), not older or other stages
        for stage, credits in (("all", 7), ("all", 40), ("normalize", 99)):
            s.add(
                CrawlRunRow(
                    started_at=datetime(2026, 10, 6, 11, 0),
                    finished_at=datetime(2026, 10, 6, 11, 45),
                    stage=stage,
                    tavily_credits=credits,
                )
            )
        s.commit()
    return settings


URLS = [SHOP, ACT, ONLY_SKIPPED]


def _files(out: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(out.iterdir())}


def test_record_seed_writes_documents_changes_and_profile(crawl_db: Settings) -> None:
    out = crawl_db.seed_dir
    summary = record_seed(crawl_db, URLS, out)
    assert sorted(p.name for p in out.iterdir()) == [
        "change-001-withdrawal.json",
        "change-002-withdrawal.json",
        "change-003-shopify.json",
        "documents.jsonl",
        "profile.json",
    ]
    assert (summary.n_documents, summary.n_changes, summary.n_skipped) == (2, 3, 3)
    assert summary.line() == f"record-seed: 2 documents · 3 changes · 3 skipped → {out}"

    docs = [json.loads(line) for line in (out / "documents.jsonl").read_text().splitlines()]
    assert [d["url"] for d in docs] == [ACT, SHOP]
    assert "fetched_at" not in docs[0] and docs[0]["markdown"] == ACT_MD

    first = json.loads((out / "change-001-withdrawal.json").read_text(encoding="utf-8"))
    assert list(first) == [
        "created_at",
        "model",
        "cost_usd",
        "tavily_credits",
        "change",
        "applicability",
    ]
    assert first["created_at"] == "2026-10-02T09:00:00+00:00"
    assert first["model"] == "nvidia/Nemotron-3-Ultra-550b-a55b"
    # the source crawl's 40 Tavily credits, apportioned over the 3 recorded changes
    assert (first["cost_usd"], first["tavily_credits"]) == (0.0123, 14)
    assert first["change"] == {
        "document_url": ACT,
        "instrument": "Directive (EU) 2023/2673 — Article 1, point (3)",
        "jurisdiction": "EU",
        "summary": "Directive (EU) 2023/2673 — Article 1, point (3) summary.",
        "effective_dates": ["2026-06-19"],
        "obligations": [{"text": "Do it.", "quote": ACT_Q}],
    }
    assert first["applicability"] == {
        "decision": "applies",
        "reasons": ["You ship to DE."],
        "todos": [_todo(ACT, ACT_Q)],
    }
    second = json.loads((out / "change-002-withdrawal.json").read_text(encoding="utf-8"))
    assert second["change"]["instrument"].endswith("Article 1, point (1)")
    assert second["applicability"] == {
        "decision": "does_not",
        "reasons": ["You ship to DE."],
        "todos": [],
    }
    shop = json.loads((out / "change-003-shopify.json").read_text(encoding="utf-8"))
    assert shop["change"]["document_url"] == SHOP
    assert (second["tavily_credits"], shop["tavily_credits"]) == (13, 13)

    profile = json.loads((out / "profile.json").read_text(encoding="utf-8"))
    assert profile == {
        "establishment_country": "DE",
        "ship_to": ["AT", "DE"],
        "categories": ["furniture_wood"],
        "platform": "shopify",
        "company_size": None,
        "supply": None,
    }


def test_record_seed_warns_for_each_skipped_change(
    crawl_db: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="regwatch.jobs.record"):
        record_seed(crawl_db, URLS, crawl_db.seed_dir)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Directive (EU) 2023/2673 — ANNEX I" in w and "no due date" in w for w in warnings)
    assert any(
        "Directive (EU) 2023/2673 — Article 2" in w and "no applicability" in w for w in warnings
    )
    assert any("§ 356a BGB — IHK Pfalz guidance" in w for w in warnings)
    assert any(ONLY_SKIPPED in w and "dropped" in w for w in warnings)
    assert not any("— old" in w for w in warnings)  # an older content hash is not a candidate
    assert not any("profile.json" in w for w in warnings)  # written from the DB profile


def test_record_seed_matching_profile_json_is_not_flagged(
    crawl_db: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    record_seed(crawl_db, URLS, crawl_db.seed_dir)
    with caplog.at_level(logging.WARNING, logger="regwatch.jobs.record"):
        record_seed(crawl_db, URLS, crawl_db.seed_dir)
    assert not any("profile.json" in r.getMessage() for r in caplog.records)


def test_record_seed_leaves_existing_profile_and_replaces_change_files(
    crawl_db: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    out = crawl_db.seed_dir
    out.mkdir(parents=True)
    (out / "profile.json").write_text('{"keep": "me"}\n', encoding="utf-8")
    (out / "change-009-stale.json").write_text("{}", encoding="utf-8")
    (out / "notes.txt").write_text("untouched", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="regwatch.jobs.record"):
        record_seed(crawl_db, URLS, out)
    assert any("profile.json" in r.getMessage() for r in caplog.records)  # kept, but flagged
    assert (out / "profile.json").read_text(encoding="utf-8") == '{"keep": "me"}\n'
    assert not (out / "change-009-stale.json").exists()
    assert (out / "notes.txt").read_text(encoding="utf-8") == "untouched"


def test_record_seed_is_deterministic(crawl_db: Settings) -> None:
    out = crawl_db.seed_dir
    record_seed(crawl_db, URLS, out)
    before = _files(out)
    record_seed(crawl_db, list(reversed(URLS)), out)
    assert _files(out) == before


def _write_sentinel(out: Path) -> dict[str, bytes]:
    out.mkdir(parents=True)
    (out / "change-001-old.json").write_text('{"old": true}', encoding="utf-8")
    (out / "documents.jsonl").write_text("old\n", encoding="utf-8")
    return _files(out)


def test_record_seed_refuses_an_ungrounded_change(crawl_db: Settings) -> None:
    with Session(make_engine(crawl_db)) as s:
        row = s.exec(select(RegChangeRow).where(RegChangeRow.document_url == SHOP)).one()
        row.obligations = json.dumps([{"text": "Do it.", "quote": "Not in the source at all."}])
        s.add(row)
        s.commit()
    before = _write_sentinel(crawl_db.seed_dir)
    with pytest.raises(RecordError, match="Not in the source at all"):
        record_seed(crawl_db, URLS, crawl_db.seed_dir)
    assert _files(crawl_db.seed_dir) == before


def test_record_seed_refuses_an_ungrounded_todo(crawl_db: Settings) -> None:
    with Session(make_engine(crawl_db)) as s:
        ch = s.exec(select(RegChangeRow).where(RegChangeRow.document_url == SHOP)).one()
        appl = s.exec(select(ApplicabilityRow).where(ApplicabilityRow.regchange_id == ch.id)).one()
        appl.todos = json.dumps([_todo(SHOP, "An invented quote.")])
        s.add(appl)
        s.commit()
    before = _write_sentinel(crawl_db.seed_dir)
    with pytest.raises(RecordError, match="An invented quote"):
        record_seed(crawl_db, URLS, crawl_db.seed_dir)
    assert _files(crawl_db.seed_dir) == before


def test_record_seed_refuses_a_url_missing_from_the_db(crawl_db: Settings) -> None:
    before = _write_sentinel(crawl_db.seed_dir)
    with pytest.raises(RecordError, match="https://example.eu/missing"):
        record_seed(crawl_db, [*URLS, "https://example.eu/missing"], crawl_db.seed_dir)
    assert _files(crawl_db.seed_dir) == before


def test_seed_from_recorded_dir_reproduces_changes_and_is_idempotent(
    crawl_db: Settings, tmp_path: Path
) -> None:
    record_seed(crawl_db, URLS, crawl_db.seed_dir)
    fresh = Settings(
        REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'fresh.db'}",
        REGWATCH_SEED_DIR=str(crawl_db.seed_dir),
    )
    assert seed(fresh) == 3
    assert seed(fresh) == 0

    def snapshot(settings: Settings, urls: list[str]) -> list[tuple[object, ...]]:
        with Session(make_engine(settings)) as s:
            prof = s.exec(select(ProfileRow).order_by(col(ProfileRow.id).desc())).first()
            assert prof is not None
            rows = s.exec(
                select(RegChangeRow, ApplicabilityRow)
                .join(ApplicabilityRow, col(ApplicabilityRow.regchange_id) == col(RegChangeRow.id))
                .where(ApplicabilityRow.profile_id == prof.id)
                .where(col(RegChangeRow.document_url).in_(urls))
            ).all()
            out: list[tuple[object, ...]] = []
            for ch, ap in rows:
                doc = s.get(DocumentRow, ch.document_url)
                if doc is None or doc.content_hash != ch.content_hash:
                    continue
                if ap.decision == "unclear":  # the only unclear rows here are the skipped ones
                    continue
                out.append(
                    (
                        ch.document_url,
                        ch.content_hash,
                        ch.instrument,
                        ch.to_schema().model_dump(mode="json"),
                        ap.to_schema().model_dump(mode="json"),
                        ap.model,
                        ap.cost_usd,
                    )
                )
            return sorted(out, key=lambda t: (str(t[0]), str(t[2])))

    recorded = snapshot(crawl_db, [ACT, SHOP])
    recorded = [r for r in recorded if r[2] != "Directive (EU) 2023/2673 — Article 2"]
    assert snapshot(fresh, [ACT, SHOP]) == recorded
    assert len(recorded) == 3


def test_cli_record_seed(crawl_db: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", crawl_db.db_url)
    out = crawl_db.seed_dir
    args = ["record-seed", "--out", str(out)]
    for url in URLS:
        args += ["--url", url]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert f"record-seed: 2 documents · 3 changes · 3 skipped → {out}" in result.output
    assert (out / "change-003-shopify.json").exists()

    result = CliRunner().invoke(app, [*args, "--url", "https://example.eu/missing"])
    assert result.exit_code == 1
    assert "https://example.eu/missing" in result.output


def test_cli_record_seed_defaults_to_settings_seed_dir(
    crawl_db: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", crawl_db.db_url)
    monkeypatch.setenv("REGWATCH_SEED_DIR", str(crawl_db.seed_dir))
    result = CliRunner().invoke(app, ["record-seed", "--url", SHOP])
    assert result.exit_code == 0, result.output
    assert (crawl_db.seed_dir / "change-001-shopify.json").exists()


def test_record_seed_without_a_stage_all_run_records_zero_credits(
    crawl_db: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with Session(make_engine(crawl_db)) as s:
        for run in s.exec(select(CrawlRunRow).where(CrawlRunRow.stage == "all")).all():
            s.delete(run)
        s.commit()
    with caplog.at_level(logging.WARNING, logger="regwatch.jobs.record"):
        record_seed(crawl_db, URLS, crawl_db.seed_dir)
    assert any('no stage "all" crawl run' in r.getMessage() for r in caplog.records)
    credits = [
        json.loads(p.read_text(encoding="utf-8"))["tavily_credits"]
        for p in sorted(crawl_db.seed_dir.glob("change-*.json"))
    ]
    assert credits == [0, 0, 0]


def test_seeded_footer_shows_the_source_crawls_credits(
    crawl_db: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from fastapi.testclient import TestClient

    record_seed(crawl_db, URLS, crawl_db.seed_dir)
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'footer.db'}")
    monkeypatch.setenv("REGWATCH_SEED_DIR", str(crawl_db.seed_dir))
    import regwatch.app as appmod

    appmod = importlib.reload(appmod)
    assert seed() == 3
    html = TestClient(appmod.app).get("/feed").text
    assert "Showing the recorded seed run" in html
    assert "40 Tavily credits used when it was recorded" in html


def test_record_seed_excludes_named_instruments(
    crawl_db: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    out = crawl_db.seed_dir
    point_1 = "Directive (EU) 2023/2673 — Article 1, point (1)"
    with caplog.at_level(logging.WARNING, logger="regwatch.jobs.record"):
        summary = record_seed(crawl_db, URLS, out, exclude=[point_1])
    assert (summary.n_documents, summary.n_changes, summary.n_skipped) == (2, 2, 4)
    assert sorted(p.name for p in out.glob("change-*.json")) == [
        "change-001-withdrawal.json",
        "change-002-shopify.json",
    ]
    instruments = [
        json.loads(p.read_text(encoding="utf-8"))["change"]["instrument"]
        for p in sorted(out.glob("change-*.json"))
    ]
    assert point_1 not in instruments
    assert any(point_1 in r.getMessage() and "excluded" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "exclude",
    [
        "Directive (EU) 2023/2673 — Article 1, point (9)",  # a typo matches nothing
        "Directive (EU) 2023/2673 — Article 1",  # exact match only, never a prefix
        "Regulation (EU) 2023/1115",  # a change of a URL that was not chosen
    ],
)
def test_record_seed_refuses_an_exclude_that_matches_nothing(
    crawl_db: Settings, exclude: str
) -> None:
    before = _write_sentinel(crawl_db.seed_dir)
    with pytest.raises(RecordError, match="matches no change"):
        record_seed(crawl_db, URLS, crawl_db.seed_dir, exclude=[exclude])
    assert _files(crawl_db.seed_dir) == before


def test_record_seed_refuses_a_todo_citing_another_url(crawl_db: Settings) -> None:
    with Session(make_engine(crawl_db)) as s:
        ch = s.exec(select(RegChangeRow).where(RegChangeRow.document_url == SHOP)).one()
        appl = s.exec(select(ApplicabilityRow).where(ApplicabilityRow.regchange_id == ch.id)).one()
        appl.todos = json.dumps([_todo(ACT, SHOP_Q)])  # grounded quote, wrong citation
        s.add(appl)
        s.commit()
    before = _write_sentinel(crawl_db.seed_dir)
    with pytest.raises(RecordError, match="not the source"):
        record_seed(crawl_db, URLS, crawl_db.seed_dir)
    assert _files(crawl_db.seed_dir) == before


def test_cli_record_seed_exclude(crawl_db: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", crawl_db.db_url)
    out = crawl_db.seed_dir
    args = ["record-seed", "--out", str(out), "--url", ACT, "--url", SHOP]
    point_1 = "Directive (EU) 2023/2673 — Article 1, point (1)"
    result = CliRunner().invoke(app, [*args, "--exclude", point_1])
    assert result.exit_code == 0, result.output
    assert f"record-seed: 2 documents · 2 changes · 3 skipped → {out}" in result.output
    result = CliRunner().invoke(app, [*args, "--exclude", "No such instrument"])
    assert result.exit_code == 1
    assert "No such instrument" in result.output


# ---------- --append: add to an existing seed without touching it ----------


def test_record_seed_append_keeps_existing_files_and_numbers_after_them(
    crawl_db: Settings,
) -> None:
    out = crawl_db.seed_dir
    record_seed(crawl_db, [SHOP], out)  # the "existing" seed: change-001-shopify + 1 document
    (out / "change-007-other.json").write_bytes((out / "change-001-shopify.json").read_bytes())
    old_change = (out / "change-001-shopify.json").read_bytes()
    old_docs = (out / "documents.jsonl").read_bytes()

    summary = record_seed(crawl_db, [ACT], out, append=True)

    assert (summary.n_documents, summary.n_changes) == (1, 2)
    assert (out / "change-001-shopify.json").read_bytes() == old_change
    assert sorted(p.name for p in out.glob("change-*.json")) == [
        "change-001-shopify.json",
        "change-007-other.json",
        "change-008-withdrawal.json",
        "change-009-withdrawal.json",
    ]
    lines = (out / "documents.jsonl").read_bytes().splitlines(keepends=True)
    assert [json.loads(line)["url"] for line in lines] == [ACT, SHOP]  # still sorted by url
    assert old_docs in lines  # the existing line is byte-identical
    # the source crawl's 40 credits are apportioned over the appended files only
    added = [json.loads((out / f"change-00{n}-withdrawal.json").read_text()) for n in (8, 9)]
    assert [a["tavily_credits"] for a in added] == [20, 20]

    fresh = Settings(
        REGWATCH_DB_URL=f"sqlite:///{out.parent / 'fresh.db'}", REGWATCH_SEED_DIR=str(out)
    )
    assert seed(fresh) == 3  # 001 and its copy 007 are one (document_url, instrument)
    assert seed(fresh) == 0


def test_record_seed_append_refuses_a_change_already_in_the_seed(crawl_db: Settings) -> None:
    out = crawl_db.seed_dir
    record_seed(crawl_db, [SHOP, ACT], out)
    before = _files(out)
    with pytest.raises(RecordError, match="already in the seed"):
        record_seed(crawl_db, [ACT], out, append=True)
    assert _files(out) == before


def test_record_seed_append_refuses_a_document_with_another_hash(crawl_db: Settings) -> None:
    out = crawl_db.seed_dir
    record_seed(crawl_db, [SHOP], out)
    for path in out.glob("change-*.json"):
        path.unlink()  # so only the documents.jsonl conflict remains
    line = json.loads((out / "documents.jsonl").read_text(encoding="utf-8"))
    line["content_hash"] = "hash-other"
    (out / "documents.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    before = _files(out)
    with pytest.raises(RecordError, match="another content hash"):
        record_seed(crawl_db, [SHOP], out, append=True)
    assert _files(out) == before


def test_cli_record_seed_append(crawl_db: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", crawl_db.db_url)
    out = crawl_db.seed_dir
    base = ["record-seed", "--out", str(out)]
    assert CliRunner().invoke(app, [*base, "--url", SHOP]).exit_code == 0
    result = CliRunner().invoke(app, [*base, "--append", "--url", ACT])
    assert result.exit_code == 0, result.output
    assert (out / "change-001-shopify.json").exists()
    assert (out / "change-003-withdrawal.json").exists()
