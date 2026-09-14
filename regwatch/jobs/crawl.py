"""The scheduled job (Nebius Serverless Job): search → extract → diff → normalize → classify → store.

Today `--stage ingest` works end to end (search + extract + diff, with a `CrawlRunRow` per run and
`examples/seed-run/documents.jsonl` as the recorded output); `--stage all` runs ingest and then
stops with NotImplementedError until Oct 19–20. `seed` loads examples/seed-run/*.json and, if
present, documents.jsonl so the demo URL is never empty.
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

from sqlmodel import col, select

from regwatch.config import Settings
from regwatch.db import (
    ApplicabilityRow,
    CrawlRunRow,
    ProfileRow,
    RegChangeRow,
    make_engine,
    session,
)
from regwatch.ingest.diff import upsert_documents
from regwatch.ingest.extract import extract_documents
from regwatch.ingest.search import search_all
from regwatch.ingest.tavily_backend import CreditLedger, TavilyBackend, make_backend
from regwatch.schemas import Applicability, Document, Profile, RegChange

log = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parents[2] / "examples" / "seed-run"
DEFAULT_OUT = SEED_DIR / "documents.jsonl"


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
    out: Path | None = DEFAULT_OUT,
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
        s.add(
            CrawlRunRow(
                started_at=started,
                finished_at=datetime.now(UTC),
                stage="ingest",
                tavily_credits=ledger.credits,
                n_search_calls=ledger.n_search_calls,
                n_extract_calls=ledger.n_extract_calls,
                n_docs_new=len(diff.new),
                n_docs_changed=len(diff.changed),
                n_docs_unchanged=len(diff.unchanged),
            )
        )
        s.commit()
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
    )


def seed(settings: Settings | None = None) -> int:
    """Load the recorded seed run. Idempotent: skips changes whose document_url already exists.

    Also upserts examples/seed-run/documents.jsonl (by url + content hash) when present.
    Returns the number of changes added.
    """
    settings = settings or Settings()
    engine = make_engine(settings)
    profile = Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    added = 0
    with session(engine) as s:
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
        for path in sorted(SEED_DIR.glob("change-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            change = RegChange.model_validate(payload["change"])
            appl = Applicability.model_validate(payload["applicability"])
            if s.exec(
                select(RegChangeRow).where(RegChangeRow.document_url == change.document_url)
            ).first():
                continue
            if appl.ungrounded_todos(change):
                raise ValueError(f"{path.name}: to-do quote not grounded in obligations")
            row = RegChangeRow(
                document_url=change.document_url,
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
        if DEFAULT_OUT.exists():
            diff = upsert_documents(s, read_documents_jsonl(DEFAULT_OUT))
            log.info(
                "seed documents: %d new, %d changed, %d unchanged",
                len(diff.new),
                len(diff.changed),
                len(diff.unchanged),
            )
    return added


def crawl(
    settings: Settings | None = None,
    *,
    stage: str = "all",
    backend: TavilyBackend | None = None,
    time_range: str = "year",
    out: Path | None = DEFAULT_OUT,
) -> IngestSummary:
    if stage not in ("ingest", "all"):
        raise ValueError(f"unknown stage {stage!r}; expected 'ingest' or 'all'")
    summary = run_ingest(settings, backend=backend, time_range=time_range, out=out)
    if stage == "all":
        log.info("%s", summary.line())
        raise NotImplementedError("Oct 19–20 — normalize → classify → store")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m regwatch.jobs.crawl")
    parser.add_argument("--stage", choices=["ingest", "all"], default="all")
    parser.add_argument("--backend", choices=["live", "fixtures"], default="live")
    parser.add_argument("--fixtures-dir", type=Path, default=None)
    parser.add_argument("--time-range", default="year")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-out", action="store_true", help="do not write documents.jsonl")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = Settings()
    kind = cast(Literal["live", "fixtures"], args.backend)
    backend = make_backend(settings, kind, args.fixtures_dir)
    out = None if args.no_out else args.out
    try:
        summary = crawl(
            settings, stage=args.stage, backend=backend, time_range=args.time_range, out=out
        )
    except NotImplementedError as e:
        print(f"crawl: {e}", file=sys.stderr)
        return 2
    print(summary.line(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
