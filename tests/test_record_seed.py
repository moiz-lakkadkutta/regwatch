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
from regwatch.db import ApplicabilityRow, DocumentRow, ProfileRow, RegChangeRow, make_engine
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
    assert (first["cost_usd"], first["tavily_credits"]) == (0.0123, 0)
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

    profile = json.loads((out / "profile.json").read_text(encoding="utf-8"))
    assert profile == {
        "establishment_country": "DE",
        "ship_to": ["AT", "DE"],
        "categories": ["furniture_wood"],
        "platform": "shopify",
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


def test_record_seed_leaves_existing_profile_and_replaces_change_files(crawl_db: Settings) -> None:
    out = crawl_db.seed_dir
    out.mkdir(parents=True)
    (out / "profile.json").write_text('{"keep": "me"}\n', encoding="utf-8")
    (out / "change-009-stale.json").write_text("{}", encoding="utf-8")
    (out / "notes.txt").write_text("untouched", encoding="utf-8")
    record_seed(crawl_db, URLS, out)
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
