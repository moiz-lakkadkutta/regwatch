"""The scheduled job (Nebius Serverless Job): search → extract → diff → normalize → classify → store.

`--stage ingest` (search + extract + diff), `--stage normalize` (Lightning → grounded RegChange per
new document) and `--stage classify` (Ultra → grounded Applicability per change for the current
profile) each work end to end with a `CrawlRunRow`; `--stage all` runs the three on one row. `seed`
loads `Settings.seed_dir` (`REGWATCH_SEED_DIR`, default examples/seed-run): *.json and
documents.jsonl, so the demo URL is never empty.
No crawl writes `examples/seed-run` by default: `regwatch record-seed` owns the seed; ingest writes a
documents.jsonl only where `--out` points.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from sqlmodel import Session, col, select

from regwatch.config import DEFAULT_SEED_DIR, Settings
from regwatch.db import (
    ApplicabilityRow,
    CrawlRunRow,
    DocumentRow,
    ProfileRow,
    RegChangeRow,
    current_profile,
    make_engine,
    session,
)
from regwatch.ingest.diff import upsert_documents
from regwatch.ingest.extract import extract_documents
from regwatch.ingest.search import search_all
from regwatch.ingest.tavily_backend import CreditLedger, TavilyBackend, make_backend
from regwatch.schemas import Applicability, Document, Profile, RegChange
from regwatch.understand.classify import ClassifySummary, classify_all
from regwatch.understand.llm_backend import LLMBackend, ModelLedger, make_llm_backend
from regwatch.understand.normalize import NormalizeSummary, normalize_all

log = logging.getLogger(__name__)

SEED_DIR = DEFAULT_SEED_DIR  # tests import it; `seed()` itself reads `settings.seed_dir`
DEFAULT_OUT = SEED_DIR / "documents.jsonl"  # the seed's documents (record-seed writes it)
Stage = Literal["all", "ingest", "normalize", "classify"]
STAGES: tuple[Stage, ...] = ("all", "ingest", "normalize", "classify")


@dataclass
class IngestSummary:
    n_docs: int
    new: int
    changed: int
    unchanged: int
    credits: int
    n_search_calls: int
    n_extract_calls: int
    out: Path | None
    run_id: int | None = None

    def line(self) -> str:
        parts = [
            f"ingest: {self.n_docs} documents "
            f"({self.new} new, {self.changed} changed, {self.unchanged} unchanged)",
            f"{self.credits} Tavily credits",
            f"{self.n_search_calls} search{'es' if self.n_search_calls != 1 else ''}",
            f"{self.n_extract_calls} extract{'s' if self.n_extract_calls != 1 else ''}",
        ]
        if self.out is not None:
            parts.append(f"wrote {_display_path(self.out)}")
        return " · ".join(parts)

    @classmethod
    def from_run(cls, run: CrawlRunRow, out: Path | None) -> IngestSummary:
        return cls(
            n_docs=run.n_docs_new + run.n_docs_changed + run.n_docs_unchanged,
            new=run.n_docs_new,
            changed=run.n_docs_changed,
            unchanged=run.n_docs_unchanged,
            credits=run.tavily_credits,
            n_search_calls=run.n_search_calls,
            n_extract_calls=run.n_extract_calls,
            out=out,
            run_id=run.id,
        )


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def write_documents_jsonl(docs: Sequence[Document], out: Path) -> None:
    """One Document per line, sorted by url, without `fetched_at` so re-runs are byte-identical."""
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            d.model_dump(mode="json", exclude={"fetched_at"}), sort_keys=True, ensure_ascii=False
        )
        + "\n"
        for d in sorted(docs, key=lambda d: d.url)
    ]
    out.write_text("".join(lines), encoding="utf-8")


def read_documents_jsonl(path: Path) -> list[Document]:
    now = datetime.now(UTC)
    docs: list[Document] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        docs.append(Document.model_validate({**payload, "fetched_at": now}))
    return docs


def run_ingest(
    settings: Settings | None = None,
    *,
    backend: TavilyBackend | None = None,
    time_range: str = "year",
    out: Path | None = None,
    stage: Stage = "ingest",
) -> IngestSummary:
    """Search → extract → diff. Persists a `CrawlRunRow` with the Tavily credits used."""
    settings = settings or Settings()
    backend = backend or make_backend(settings, "live")
    engine = make_engine(settings)
    started = datetime.now(UTC)
    ledger = CreditLedger()
    hits = search_all(backend, ledger=ledger, time_range=time_range)
    docs = extract_documents(backend, hits, ledger=ledger, fetched_at=started)
    docs.sort(key=lambda d: d.url)
    with session(engine) as s:
        diff = upsert_documents(s, docs)
        run = CrawlRunRow(
            started_at=started,
            finished_at=datetime.now(UTC),
            stage=stage,
            tavily_credits=ledger.credits,
            n_search_calls=ledger.n_search_calls,
            n_extract_calls=ledger.n_extract_calls,
            n_docs_new=len(diff.new),
            n_docs_changed=len(diff.changed),
            n_docs_unchanged=len(diff.unchanged),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
    if out is not None:
        write_documents_jsonl(docs, out)
    return IngestSummary(
        n_docs=len(docs),
        new=len(diff.new),
        changed=len(diff.changed),
        unchanged=len(diff.unchanged),
        credits=ledger.credits,
        n_search_calls=ledger.n_search_calls,
        n_extract_calls=ledger.n_extract_calls,
        out=out,
        run_id=run_id,
    )


def run_normalize(
    settings: Settings | None = None,
    *,
    llm_backend: LLMBackend,
    run_id: int | None = None,
) -> tuple[NormalizeSummary, CrawlRunRow]:
    """Normalise every document without a stored change for its content hash.

    Updates the `CrawlRunRow` `run_id` (the ingest of a `--stage all` run) or creates a
    stage="normalize" row; model cost and the normalize counters are persisted on it.
    """
    settings = settings or Settings()
    engine = make_engine(settings)
    ledger = ModelLedger.for_extractor(settings)
    with session(engine) as s:
        run = s.get(CrawlRunRow, run_id) if run_id is not None else None
        if run is None:
            run = CrawlRunRow(started_at=datetime.now(UTC), stage="normalize")
            s.add(run)
            s.commit()
            s.refresh(run)
        summary = normalize_all(s, backend=llm_backend, settings=settings, ledger=ledger)
        run.model_cost_usd += ledger.cost_usd
        run.n_changes_new = summary.n_changes_new
        run.n_normalize_retried = summary.n_retried
        run.n_normalize_failed = summary.n_failed
        run.finished_at = datetime.now(UTC)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.expunge(run)
    return summary, run


def run_classify(settings: Settings, s: Session, *, llm_backend: LLMBackend) -> ClassifySummary:
    """Classify every stored change without an ApplicabilityRow for the current profile.

    No profile yet → WARNING and an empty summary (nothing to classify against).
    """
    profile = current_profile(s)
    if profile is None:
        log.warning("classify: no profile yet — open /profile")
        return ClassifySummary.empty()
    ledger = ModelLedger.for_classifier(settings)
    return classify_all(
        s, profile_row=profile, backend=llm_backend, settings=settings, ledger=ledger
    )


def _classify_stage(
    settings: Settings, *, llm_backend: LLMBackend, run_id: int | None = None
) -> tuple[ClassifySummary, CrawlRunRow]:
    """`run_classify` on the `CrawlRunRow` `run_id` (a `--stage all` run) or a new stage="classify"
    row; classify counters and model cost are persisted on it."""
    engine = make_engine(settings)
    with session(engine) as s:
        run = s.get(CrawlRunRow, run_id) if run_id is not None else None
        if run is None:
            run = CrawlRunRow(started_at=datetime.now(UTC), stage="classify")
            s.add(run)
            s.commit()
            s.refresh(run)
        summary = run_classify(settings, s, llm_backend=llm_backend)
        run.model_cost_usd += summary.cost_usd
        run.classify_cost_usd = summary.cost_usd
        run.n_classified = summary.n_applies + summary.n_does_not + summary.n_unclear
        run.n_applies = summary.n_applies
        run.n_does_not = summary.n_does_not
        run.n_unclear = summary.n_unclear
        run.n_classify_retried = summary.n_retried
        run.n_classify_failed = summary.n_failed
        run.n_todos = summary.n_todos
        run.n_todos_dropped = summary.n_todos_dropped
        run.finished_at = datetime.now(UTC)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.expunge(run)
    return summary, run


def seed(settings: Settings | None = None) -> int:
    """Load the recorded seed run. Idempotent: skips changes whose (document_url, instrument)
    already exists, so several sections of one act (`"<act> — <label>"`) all load once.

    Upserts examples/seed-run/documents.jsonl first (by url + content hash) so each seed change
    carries the content_hash of its source document and `--stage normalize` leaves it alone.
    Returns the number of changes added.
    """
    settings = settings or Settings()
    engine = make_engine(settings)
    seed_dir = settings.seed_dir
    profile = Profile.model_validate_json((seed_dir / "profile.json").read_text(encoding="utf-8"))
    added = 0
    with session(engine) as s:
        documents_jsonl = seed_dir / "documents.jsonl"
        if documents_jsonl.exists():
            diff = upsert_documents(s, read_documents_jsonl(documents_jsonl))
            log.info(
                "seed documents: %d new, %d changed, %d unchanged",
                len(diff.new),
                len(diff.changed),
                len(diff.unchanged),
            )
        prof = s.exec(select(ProfileRow).order_by(col(ProfileRow.id).desc())).first()
        if prof is None:
            prof = ProfileRow(
                establishment_country=profile.establishment_country,
                ship_to=json.dumps(profile.ship_to),
                categories=json.dumps(profile.categories),
                platform=profile.platform,
            )
            s.add(prof)
            s.commit()
            s.refresh(prof)
        assert prof.id is not None
        for path in sorted(seed_dir.glob("change-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            change = RegChange.model_validate(payload["change"])
            appl = Applicability.model_validate(payload["applicability"])
            doc = s.get(DocumentRow, change.document_url)
            content_hash = doc.content_hash if doc is not None else ""
            existing = s.exec(
                select(RegChangeRow).where(
                    RegChangeRow.document_url == change.document_url,
                    RegChangeRow.instrument == change.instrument,
                )
            ).first()
            if existing is not None:
                if not existing.content_hash and content_hash:
                    existing.content_hash = content_hash
                    s.add(existing)
                    s.commit()
                continue
            if appl.ungrounded_todos(change):
                raise ValueError(f"{path.name}: to-do quote not grounded in obligations")
            row = RegChangeRow(
                document_url=change.document_url,
                content_hash=content_hash,
                model="seed",
                instrument=change.instrument,
                jurisdiction=change.jurisdiction,
                summary=change.summary,
                effective_dates=json.dumps([d.isoformat() for d in change.effective_dates]),
                obligations=json.dumps([o.model_dump() for o in change.obligations]),
                created_at=datetime.fromisoformat(
                    payload.get("created_at", datetime.now(UTC).isoformat())
                ),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            assert row.id is not None
            s.add(
                ApplicabilityRow(
                    profile_id=prof.id,
                    regchange_id=row.id,
                    decision=appl.decision,
                    reasons=json.dumps(appl.reasons),
                    todos=json.dumps([t.model_dump(mode="json") for t in appl.todos]),
                    model=payload.get("model", settings.classifier_model),
                    cost_usd=float(payload.get("cost_usd", 0.0)),
                    tavily_credits=int(payload.get("tavily_credits", 0)),
                )
            )
            s.commit()
            added += 1
    return added


def _load_run(settings: Settings, run_id: int | None) -> CrawlRunRow:
    assert run_id is not None
    with session(make_engine(settings)) as s:
        run = s.get(CrawlRunRow, run_id)
        assert run is not None
        s.expunge(run)
    return run


def crawl(
    settings: Settings | None = None,
    *,
    stage: Stage = "all",
    backend: TavilyBackend | None = None,
    llm_backend: LLMBackend | None = None,
    classify_backend: LLMBackend | None = None,
    llm_fixtures_dir: Path | None = None,
    time_range: str = "year",
    out: Path | None = None,
) -> CrawlRunRow:
    """`run_crawl` without the in-memory normalize summary."""
    run, _ = run_crawl(
        settings,
        stage=stage,
        backend=backend,
        llm_backend=llm_backend,
        classify_backend=classify_backend,
        llm_fixtures_dir=llm_fixtures_dir,
        time_range=time_range,
        out=out,
    )
    return run


def run_crawl(
    settings: Settings | None = None,
    *,
    stage: Stage = "all",
    backend: TavilyBackend | None = None,
    llm_backend: LLMBackend | None = None,
    classify_backend: LLMBackend | None = None,
    llm_fixtures_dir: Path | None = None,
    time_range: str = "year",
    out: Path | None = None,
) -> tuple[CrawlRunRow, NormalizeSummary | None]:
    """Run one stage (or all); return its `CrawlRunRow` and, when normalize ran, its summary
    (the row stores changes, not documents, so only the summary can print the normalize line).

    "ingest": search → extract → diff. "normalize": no ingest; Lightning over the DB. "classify":
    Ultra over the stored changes for the current profile. "all": the three on one row.
    `classify_backend` defaults to `llm_backend` (one live backend serves both stages; fixture
    backends are per stage, see `make_llm_backend(stage=...)`).
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    settings = settings or Settings()
    if stage == "ingest":
        ingest = run_ingest(settings, backend=backend, time_range=time_range, out=out)
        return _load_run(settings, ingest.run_id), None
    llm = llm_backend or make_llm_backend("live", settings, llm_fixtures_dir)
    classify_llm = classify_backend or llm
    if stage == "normalize":
        summary, run = run_normalize(settings, llm_backend=llm)
        return run, summary
    if stage == "classify":
        _, run = _classify_stage(settings, llm_backend=classify_llm)
        return run, None
    ingest = run_ingest(settings, backend=backend, time_range=time_range, out=out, stage="all")
    summary, _ = run_normalize(settings, llm_backend=llm, run_id=ingest.run_id)
    _, run = _classify_stage(settings, llm_backend=classify_llm, run_id=ingest.run_id)
    return run, summary


def summary_lines(
    run: CrawlRunRow, out: Path | None, normalize: NormalizeSummary | None = None
) -> list[str]:
    """The stderr lines for a finished run (one per stage, plus a total for `all`). The normalize
    line is `normalize.line()` when the in-memory summary is given; rebuilt from the row alone it
    reports changes, never documents (one long act stores several changes)."""
    lines: list[str] = []
    if run.stage in ("ingest", "all"):
        lines.append(IngestSummary.from_run(run, out).line())
    if run.stage in ("normalize", "all"):
        if normalize is not None:
            lines.append(normalize.line())
        else:
            lines.append(
                f"normalize: {run.n_changes_new} new changes · {run.n_normalize_retried} "
                f"retried · {run.n_normalize_failed} failed · "
                f"${run.model_cost_usd - run.classify_cost_usd:.4f} model"
            )
    if run.stage in ("classify", "all"):
        lines.append(
            ClassifySummary(
                n_changes=run.n_classified + run.n_classify_failed,
                n_applies=run.n_applies,
                n_does_not=run.n_does_not,
                n_unclear=run.n_unclear,
                n_retried=run.n_classify_retried,
                n_failed=run.n_classify_failed,
                n_todos=run.n_todos,
                n_todos_dropped=run.n_todos_dropped,
                cost_usd=run.classify_cost_usd,
            ).line()
        )
    if run.stage == "all":
        lines.append(
            f"crawl: {run.n_docs_new} new documents · {run.n_changes_new} new changes · "
            f"{run.n_classified} classified · {run.tavily_credits} Tavily credits · "
            f"${run.model_cost_usd:.4f} model"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m regwatch.jobs.crawl")
    parser.add_argument("--stage", choices=list(STAGES), default="all")
    parser.add_argument("--backend", choices=["live", "fixtures"], default="live")
    parser.add_argument("--fixtures-dir", type=Path, default=None)
    parser.add_argument("--llm-backend", choices=["live", "fixtures"], default="live")
    parser.add_argument("--llm-fixtures-dir", type=Path, default=None)
    parser.add_argument("--time-range", default="year")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the ingested documents.jsonl here (default: nothing is written)",
    )
    parser.add_argument(
        "--no-out", action="store_true", help="do not write documents.jsonl (the default)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = Settings()
    stage = cast(Stage, args.stage)
    backend: TavilyBackend | None = None
    if stage in ("ingest", "all"):
        kind = cast(Literal["live", "fixtures"], args.backend)
        backend = make_backend(settings, kind, args.fixtures_dir)
    llm: LLMBackend | None = None
    classify_llm: LLMBackend | None = None
    if stage != "ingest":
        llm_kind = cast(Literal["live", "fixtures"], args.llm_backend)
        if llm_kind == "live":
            llm = classify_llm = make_llm_backend("live", settings)
        else:
            llm = make_llm_backend("fixtures", settings, args.llm_fixtures_dir, stage="normalize")
            classify_llm = make_llm_backend(
                "fixtures", settings, args.llm_fixtures_dir, stage="classify"
            )
    out: Path | None = None if args.no_out else args.out
    run, normalize_summary = run_crawl(
        settings,
        stage=stage,
        backend=backend,
        llm_backend=llm,
        classify_backend=classify_llm,
        time_range=args.time_range,
        out=out,
    )
    for line in summary_lines(run, out, normalize_summary):
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
