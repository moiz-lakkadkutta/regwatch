import json
import logging
import re
import shutil
from pathlib import Path

import pytest
from sqlmodel import Session, col, select

from regwatch.config import Settings
from regwatch.db import (
    ApplicabilityRow,
    CrawlRunRow,
    DocumentRow,
    ProfileRow,
    RegChangeRow,
    make_engine,
    regchanges_needing_classify,
)
from regwatch.jobs.crawl import SEED_DIR, crawl, main, seed, summary_lines
from regwatch.schemas import Profile
from regwatch.understand.llm_backend import FixtureLLMBackend
from tests.conftest import LLM_FIXTURES_ROOT
from tests.fakes import RecordingBackend


def _seed_profile(settings: Settings) -> int:
    """Only the ProfileRow from examples/seed-run (not the seed changes), like a fresh /profile."""
    profile = Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    with Session(make_engine(settings)) as s:
        row = ProfileRow(
            establishment_country=profile.establishment_country,
            ship_to=json.dumps(profile.ship_to),
            categories=json.dumps(profile.categories),
            platform=profile.platform,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.id is not None
        return row.id


def _backends() -> tuple[FixtureLLMBackend, FixtureLLMBackend]:
    return (
        FixtureLLMBackend(LLM_FIXTURES_ROOT / "normalize"),
        FixtureLLMBackend(LLM_FIXTURES_ROOT / "classify"),
    )


def _crawl_all(settings: Settings, tavily: RecordingBackend, out: Path) -> CrawlRunRow:
    normalize_llm, classify_llm = _backends()
    return crawl(
        settings,
        stage="all",
        backend=tavily,
        llm_backend=normalize_llm,
        classify_backend=classify_llm,
        out=out,
    )


def test_stage_all_fixtures_offline_end_to_end(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    profile_id = _seed_profile(tmp_settings)
    run = _crawl_all(tmp_settings, fake_backend, tmp_path / "docs.jsonl")
    assert run.stage == "all" and run.finished_at is not None
    assert run.n_docs_new == 16 and run.tavily_credits == 36
    assert run.n_changes_new == 15 and run.n_normalize_failed == 1
    assert run.n_classified == run.n_changes_new > 0
    assert (run.n_applies, run.n_does_not, run.n_unclear) == (3, 7, 5)
    assert (run.n_classify_retried, run.n_classify_failed) == (1, 0)
    assert (run.n_todos, run.n_todos_dropped) == (7, 0)
    assert run.model_cost_usd > run.classify_cost_usd > 0
    lines = summary_lines(run, tmp_path / "docs.jsonl")
    assert lines[2].startswith(
        "classify: 15 changes · 3 apply · 7 don't · 5 unclear · 1 retried · 0 failed · "
        "7 to-dos (0 dropped) · $"
    )
    assert lines[3].startswith("crawl: 16 new documents · 15 new changes · 15 classified · 36 Tav")
    assert lines[3].endswith(f"${run.model_cost_usd:.4f} model")
    with Session(make_engine(tmp_settings)) as s:
        rows = s.exec(select(ApplicabilityRow)).all()
        assert len(rows) == 15 and all(r.profile_id == profile_id for r in rows)
        assert all(r.model == tmp_settings.classifier_model and r.cost_usd > 0 for r in rows)
        assert sum(r.cost_usd for r in rows) == pytest.approx(run.classify_cost_usd)
        # grounding, checked by the code at the boundary: every stored to-do quotes an obligation
        for r in rows:
            change = s.get(RegChangeRow, r.regchange_id)
            assert change is not None
            appl = r.to_schema()
            assert appl.ungrounded_todos(change.to_schema()) == []
            for t in appl.todos:
                assert t.citation_url == change.document_url
        assert regchanges_needing_classify(s, profile_id) == []


def test_crawl_all_twice_is_idempotent(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    _seed_profile(tmp_settings)
    first = _crawl_all(tmp_settings, fake_backend, tmp_path / "docs.jsonl")
    second = _crawl_all(tmp_settings, fake_backend, tmp_path / "docs.jsonl")
    assert (second.n_docs_new, second.n_changes_new, second.n_classified) == (0, 0, 0)
    assert second.n_docs_unchanged == 16 and second.tavily_credits == 36
    assert second.classify_cost_usd == 0.0 and second.n_todos == 0
    assert second.n_normalize_failed == 1  # the ungrounded doc is retried every crawl (issues.md)
    assert summary_lines(second, None)[3].startswith(
        "crawl: 0 new documents · 0 new changes · 0 classified · 36 Tavily credits · $"
    )
    with Session(make_engine(tmp_settings)) as s:
        runs = s.exec(select(CrawlRunRow).order_by(col(CrawlRunRow.id))).all()
        assert [r.id for r in runs] == [first.id, second.id] and len(runs) == 2
        assert len(s.exec(select(DocumentRow)).all()) == 16
        assert len(s.exec(select(RegChangeRow)).all()) == 15
        assert len(s.exec(select(ApplicabilityRow)).all()) == 15


def test_stage_classify_only(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    _seed_profile(tmp_settings)
    crawl(tmp_settings, stage="ingest", backend=fake_backend, out=tmp_path / "docs.jsonl")
    normalize_llm, classify_llm = _backends()
    crawl(tmp_settings, stage="normalize", llm_backend=normalize_llm)
    run = crawl(tmp_settings, stage="classify", llm_backend=classify_llm)
    assert run.stage == "classify" and run.finished_at is not None
    assert run.n_classified == 15 and run.n_applies == 3
    assert run.tavily_credits == 0 and run.n_changes_new == 0
    assert run.model_cost_usd == run.classify_cost_usd > 0
    assert normalize_llm.calls and len(classify_llm.calls) == 16  # 15 changes + 1 retry
    lines = summary_lines(run, None)
    assert len(lines) == 1 and lines[0].startswith("classify: 15 changes · 3 apply")
    with Session(make_engine(tmp_settings)) as s:
        assert [r.stage for r in s.exec(select(CrawlRunRow).order_by(col(CrawlRunRow.id)))] == [
            "ingest",
            "normalize",
            "classify",
        ]


def test_stage_classify_without_profile_is_a_no_op(
    tmp_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    _, classify_llm = _backends()
    with caplog.at_level(logging.WARNING, logger="regwatch.jobs.crawl"):
        run = crawl(tmp_settings, stage="classify", llm_backend=classify_llm)
    assert run.stage == "classify" and run.n_classified == 0 and run.model_cost_usd == 0.0
    assert classify_llm.calls == []
    assert any("no profile yet" in r.message for r in caplog.records)
    assert summary_lines(run, None) == [
        "classify: 0 changes · 0 apply · 0 don't · 0 unclear · 0 retried · 0 failed · "
        "0 to-dos (0 dropped) · $0.0000"
    ]
    with Session(make_engine(tmp_settings)) as s:
        assert s.exec(select(ApplicabilityRow)).all() == []


def test_seed_then_crawl_all_leaves_seed_changes_alone(
    tmp_settings: Settings, fake_backend: RecordingBackend, tmp_path: Path
) -> None:
    assert seed(tmp_settings) == 2
    run = _crawl_all(tmp_settings, fake_backend, tmp_path / "docs.jsonl")
    # the two seed documents are unchanged and already classified for the seed profile
    assert (run.n_docs_new, run.n_docs_unchanged) == (14, 2)
    assert run.n_changes_new == 13 and run.n_classified == 13 and run.n_applies == 1
    with Session(make_engine(tmp_settings)) as s:
        rows = s.exec(select(ApplicabilityRow)).all()
        assert len(rows) == 15
        assert sum(r.model == "nvidia/Nemotron-3-Ultra-550b-a55b" for r in rows) == 15
        seed_rows = [r for r in rows if r.cost_usd in (0.06, 0.07)]
        assert len(seed_rows) == 2  # untouched, still carrying the recorded seed cost
    assert seed(tmp_settings) == 0


def test_seed_honours_seed_dir_setting(tmp_path: Path) -> None:
    """`seed()` reads Settings.seed_dir (REGWATCH_SEED_DIR), not a path relative to the package."""
    custom = tmp_path / "s"
    shutil.copytree(SEED_DIR, custom)
    (custom / "change-002-eudr.json").unlink()
    settings = Settings(
        REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'seed.db'}", REGWATCH_SEED_DIR=str(custom)
    )
    assert settings.seed_dir == custom
    assert seed(settings) == 1
    assert seed(settings) == 0
    with Session(make_engine(settings)) as s:
        assert len(s.exec(select(RegChangeRow)).all()) == 1
        assert len(s.exec(select(DocumentRow)).all()) == 2  # documents.jsonl still has both
    assert Settings(REGWATCH_DB_URL="sqlite://").seed_dir == SEED_DIR


def test_main_cli_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("REGWATCH_DB_URL", db_url)
    _seed_profile(Settings(REGWATCH_DB_URL=db_url))
    with caplog.at_level(logging.INFO, logger="regwatch.understand.classify"):
        rc = main(
            ["--stage", "all", "--backend", "fixtures", "--llm-backend", "fixtures", "--no-out"]
        )
    assert rc == 0
    err = capsys.readouterr().err
    assert re.search(r"^ingest: 16 documents \(16 new", err, re.M)
    assert re.search(r"^normalize: 16 documents · 14 ok first try", err, re.M)
    assert re.search(
        r"^classify: \d+ changes · 3 apply · 7 don't · 5 unclear · 1 retried", err, re.M
    )
    assert re.search(
        r"^crawl: 16 new documents · 15 new changes · 15 classified · 36 Tavily credits · \$\d",
        err,
        re.M,
    )
    usage_lines = [
        r.message
        for r in caplog.records
        if re.match(r"classify url=\S+ model=\S+ attempt=\d usage=\d+/\d+ cost=\$", r.message)
    ]
    assert len(usage_lines) == 16  # one INFO per Ultra call, the retry included
    assert not (tmp_path / "documents.jsonl").exists()

    rc = main(["--stage", "classify", "--llm-backend", "fixtures"])
    assert rc == 0
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith("classify: 0 changes · 0 apply")
