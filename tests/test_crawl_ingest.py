import importlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from regwatch.config import Settings
from regwatch.db import CrawlRunRow, DocumentRow, make_engine
from regwatch.jobs.crawl import (
    DEFAULT_OUT,
    crawl,
    main,
    read_documents_jsonl,
    run_ingest,
    seed,
)
from tests.fakes import RecordingBackend


def _count(settings: Settings, model: type[CrawlRunRow] | type[DocumentRow]) -> int:
    with Session(make_engine(settings)) as s:
        n = s.exec(select(func.count()).select_from(model)).one()
    return int(n)


def test_stage_ingest_yields_at_least_15(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    summary = run_ingest(tmp_settings, backend=fake_backend, out=tmp_path / "docs.jsonl")
    assert summary.n_docs >= 15
    assert summary.new == summary.n_docs
    assert summary.changed == 0 and summary.unchanged == 0
    assert _count(tmp_settings, DocumentRow) == summary.n_docs
    assert (tmp_path / "docs.jsonl").exists()
    assert summary.line().startswith(f"ingest: {summary.n_docs} documents ({summary.n_docs} new")
    assert "36 Tavily credits" in summary.line()
    assert "14 searches · 1 extract" in summary.line()


def test_crawl_run_persisted_with_credits(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    run_ingest(tmp_settings, backend=fake_backend, out=tmp_path / "docs.jsonl")
    with Session(make_engine(tmp_settings)) as s:
        runs = s.exec(select(CrawlRunRow)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.stage == "ingest"
    assert run.tavily_credits == 36
    assert run.n_search_calls == 14
    assert run.n_extract_calls == 1
    assert run.n_docs_new == 16 and run.n_docs_changed == 0 and run.n_docs_unchanged == 0
    assert run.finished_at is not None and run.finished_at >= run.started_at
    assert run.model_cost_usd == 0.0


def test_documents_jsonl_deterministic(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    out1, out2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    run_ingest(tmp_settings, backend=fake_backend, out=out1)
    run_ingest(tmp_settings, backend=fake_backend, out=out2)
    assert out1.read_bytes() == out2.read_bytes()
    lines = out1.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    payloads = [json.loads(line) for line in lines]
    urls = [p["url"] for p in payloads]
    assert urls == sorted(urls)
    assert all("fetched_at" not in p for p in payloads)
    assert all(
        set(p)
        == {"url", "content_hash", "published_date", "markdown", "source_domain", "regulation_tags"}
        for p in payloads
    )
    # the committed file is the fixture run, byte for byte
    assert out1.read_bytes() == DEFAULT_OUT.read_bytes()
    docs = read_documents_jsonl(out1)
    assert [d.url for d in docs] == urls


def test_second_run_is_idempotent(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    out = tmp_path / "docs.jsonl"
    first = run_ingest(tmp_settings, backend=fake_backend, out=out)
    second = run_ingest(tmp_settings, backend=fake_backend, out=out)
    assert (first.new, first.changed, first.unchanged) == (16, 0, 0)
    assert (second.new, second.changed, second.unchanged) == (0, 0, 16)
    assert second.line().startswith("ingest: 16 documents (0 new, 0 changed, 16 unchanged)")
    assert _count(tmp_settings, CrawlRunRow) == 2
    assert _count(tmp_settings, DocumentRow) == 16


def test_main_stage_ingest_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    out = tmp_path / "docs.jsonl"
    rc = main(["--stage", "ingest", "--backend", "fixtures", "--out", str(out)])
    assert rc == 0
    err = capsys.readouterr().err
    assert re.search(r"^ingest: \d+ documents", err, re.M)
    assert "36 Tavily credits" in err
    assert out.exists()

    rc = main(["--stage", "ingest", "--backend", "fixtures", "--no-out"])
    assert rc == 0
    err = capsys.readouterr().err
    assert re.search(r"^ingest: 16 documents \(0 new, 0 changed, 16 unchanged\)", err, re.M)
    assert "wrote" not in err.splitlines()[-1]


def test_stage_all_raises_not_implemented(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    with pytest.raises(NotImplementedError):
        crawl(tmp_settings, stage="all", backend=fake_backend, out=tmp_path / "docs.jsonl")
    # ingest ran before the stop
    assert _count(tmp_settings, CrawlRunRow) == 1
    assert _count(tmp_settings, DocumentRow) == 16
    with pytest.raises(ValueError):
        crawl(tmp_settings, stage="bogus", backend=fake_backend, out=None)


def test_seed_loads_documents_jsonl_idempotently(tmp_settings: Settings) -> None:
    n_lines = len(DEFAULT_OUT.read_text(encoding="utf-8").splitlines())
    assert n_lines >= 15
    assert seed(tmp_settings) == 2
    assert _count(tmp_settings, DocumentRow) == n_lines
    with Session(make_engine(tmp_settings)) as s:
        row = s.exec(select(DocumentRow).order_by(DocumentRow.url)).first()
        assert row is not None
        first_fetched = row.fetched_at
        assert row.to_schema().regulation_tags
    assert seed(tmp_settings) == 0
    assert _count(tmp_settings, DocumentRow) == n_lines
    with Session(make_engine(tmp_settings)) as s:
        row = s.exec(select(DocumentRow).order_by(DocumentRow.url)).first()
        assert row is not None and row.fetched_at == first_fetched  # unchanged rows untouched


def test_feed_footer_shows_last_crawl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_backend: RecordingBackend
) -> None:
    db_url = f"sqlite:///{tmp_path / 'feed.db'}"
    monkeypatch.setenv("REGWATCH_DB_URL", db_url)
    import regwatch.app as appmod

    importlib.reload(appmod)
    settings = Settings(REGWATCH_DB_URL=db_url)
    run_ingest(settings, backend=fake_backend, out=tmp_path / "docs.jsonl")
    assert seed(settings) == 2
    r = TestClient(appmod.app).get("/feed")
    assert r.status_code == 200
    assert "Last crawl" in r.text
    assert "9 sources" in r.text
    assert "36 Tavily credits used" in r.text
