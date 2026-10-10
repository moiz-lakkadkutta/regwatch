"""`regwatch record-seed`: write chosen documents of a grounded crawl as an examples/seed-run dir.

For each chosen URL: its DocumentRow → documents.jsonl, and every RegChangeRow for the document's
current content hash, with its ApplicabilityRow for the current profile → change-NNN-<slug>.json
(the payload `seed()` loads). Changes with no applicability, with a to-do without a due date, or
named by `--exclude` (exact instrument) are skipped with a WARNING — never given invented dates.
Nothing is written when a URL is missing from the DB, an exclude matches no candidate change, or any
candidate change fails grounding.

Tavily credits: live applicabilities carry `tavily_credits=0` (credits are spent per crawl, on
the `CrawlRunRow`). So that the seeded feed footer ("N Tavily credits used when it was recorded",
the sum over the seeded applicabilities) shows the source crawl's real number, the credits of the
latest stage "all" run in the DB are apportioned over the recorded change files'
`tavily_credits` (integer split, the remainder on the first files; the payload keys stay the
same). No such run → 0 and a WARNING.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlmodel import Session, col, select

from regwatch.config import Settings
from regwatch.db import (
    ApplicabilityRow,
    CrawlRunRow,
    DocumentRow,
    RegChangeRow,
    current_profile,
    make_engine,
    regulation_tag_for,
)
from regwatch.jobs.crawl import write_documents_jsonl
from regwatch.schemas import Profile

log = logging.getLogger(__name__)


class RecordError(Exception):
    """The seed cannot be recorded; nothing was written."""


@dataclass(frozen=True)
class RecordSummary:
    n_documents: int
    n_changes: int
    n_skipped: int
    out: Path

    def line(self) -> str:
        return (
            f"record-seed: {self.n_documents} documents · {self.n_changes} changes · "
            f"{self.n_skipped} skipped → {self.out}"
        )


def host_slug(url: str) -> str:
    """The registrable-name label of the host: help.shopify.com → shopify, www.ihk.de → ihk."""
    labels = [p for p in (urlparse(url).hostname or "").split(".") if p and p != "www"]
    label = labels[-2] if len(labels) >= 2 else (labels[0] if labels else "source")
    return _safe(label)


def _safe(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "source"


def _iso(dt: datetime) -> str:
    """Stored datetimes are naive UTC; the seed files carry an explicit offset."""
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)).isoformat()


def _read_profile(path: Path) -> Profile | None:
    try:
        return Profile.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def apportion(total: int, n: int) -> list[int]:
    """`total` split into `n` integers that sum to it; the remainder goes to the first ones."""
    base, rest = divmod(total, n) if n else (0, 0)
    return [base + (1 if i < rest else 0) for i in range(n)]


def _source_crawl_credits(s: Session) -> int:
    run = s.exec(
        select(CrawlRunRow).where(CrawlRunRow.stage == "all").order_by(col(CrawlRunRow.id).desc())
    ).first()
    if run is None:
        log.warning('record-seed: no stage "all" crawl run in the DB — recording 0 Tavily credits')
        return 0
    return run.tavily_credits


def _payload(ch: RegChangeRow, ap: ApplicabilityRow, credits: int) -> dict[str, Any]:
    return {
        "created_at": _iso(ch.created_at),
        "model": ap.model,
        "cost_usd": ap.cost_usd,
        "tavily_credits": credits,
        "change": ch.to_schema().model_dump(mode="json"),
        "applicability": ap.to_schema().model_dump(mode="json"),
    }


def _grounding_errors(doc: DocumentRow, ch: RegChangeRow, ap: ApplicabilityRow | None) -> list[str]:
    change = ch.to_schema()
    errors = [f"quote not in source: {q!r}" for q in change.validate_quotes(doc.markdown)]
    if ap is not None:
        appl = ap.to_schema()
        errors += [
            f"to-do quote not an obligation: {t.quote!r}" for t in appl.ungrounded_todos(change)
        ]
        errors += [
            f"to-do cites {t.citation_url!r}, not the source"
            for t in appl.todos
            if t.citation_url != change.document_url
        ]
    return errors


def _collect(
    s: Session, urls: Sequence[str], exclude: Sequence[str]
) -> tuple[list[DocumentRow], list[tuple[DocumentRow, RegChangeRow, ApplicabilityRow]], int]:
    profile = current_profile(s)
    if profile is None or profile.id is None:
        raise RecordError("no profile in the DB — open /profile and crawl first")
    missing = [u for u in urls if s.get(DocumentRow, u) is None]
    if missing:
        raise RecordError(f"not in the DB: {', '.join(missing)}")
    docs: list[DocumentRow] = []
    kept: list[tuple[DocumentRow, RegChangeRow, ApplicabilityRow]] = []
    n_skipped = 0
    errors: list[str] = []
    unmatched = set(exclude)
    for url in sorted(set(urls)):
        doc = s.get(DocumentRow, url)
        assert doc is not None
        changes = s.exec(
            select(RegChangeRow)
            .where(RegChangeRow.document_url == url, RegChangeRow.content_hash == doc.content_hash)
            .order_by(col(RegChangeRow.id))
        ).all()
        n_kept = 0
        for ch in changes:
            ap = s.exec(
                select(ApplicabilityRow)
                .where(
                    ApplicabilityRow.regchange_id == ch.id,
                    ApplicabilityRow.profile_id == profile.id,
                )
                .order_by(col(ApplicabilityRow.id).desc())
            ).first()
            errors += [f"{ch.instrument} ({url}): {e}" for e in _grounding_errors(doc, ch, ap)]
            if ch.instrument in exclude:
                unmatched.discard(ch.instrument)
                log.warning("record-seed: skip %s (%s): excluded by --exclude", ch.instrument, url)
                n_skipped += 1
                continue
            if ap is None:
                log.warning(
                    "record-seed: skip %s (%s): no applicability for the profile",
                    ch.instrument,
                    url,
                )
                n_skipped += 1
                continue
            if any(t.due is None for t in ap.to_schema().todos):
                log.warning(
                    "record-seed: skip %s (%s): a to-do has no due date", ch.instrument, url
                )
                n_skipped += 1
                continue
            kept.append((doc, ch, ap))
            n_kept += 1
        if n_kept:
            docs.append(doc)
        else:
            log.warning("record-seed: dropped %s: no recordable changes left", url)
    if unmatched:
        names = ", ".join(repr(x) for x in sorted(unmatched))
        raise RecordError(f"--exclude matches no change of the chosen URLs: {names}")
    if errors:
        raise RecordError("grounding failed:\n" + "\n".join(errors))
    if not kept:
        raise RecordError("no recordable changes for the chosen URLs")
    return docs, kept, n_skipped


def record_seed(
    settings: Settings, urls: Sequence[str], out: Path, *, exclude: Sequence[str] = ()
) -> RecordSummary:
    """Record `urls` from the DB in `settings` into `out`, leaving out the changes whose instrument
    is in `exclude`. Raises RecordError, writing nothing."""
    engine = make_engine(settings)
    with Session(engine) as s:
        docs, kept, n_skipped = _collect(s, urls, exclude)
        credits = apportion(_source_crawl_credits(s), len(kept))
        profile = current_profile(s)
        assert profile is not None
        db_profile = profile.to_schema()
        profile_json = json.dumps(db_profile.model_dump(), ensure_ascii=False) + "\n"
        files: list[tuple[str, str]] = []
        for n, (doc, ch, ap) in enumerate(kept, start=1):
            tag = regulation_tag_for(doc)
            slug = _safe(tag) if tag else host_slug(doc.url)
            body = json.dumps(_payload(ch, ap, credits[n - 1]), indent=2, ensure_ascii=False) + "\n"
            files.append((f"change-{n:03d}-{slug}.json", body))
        documents = [d.to_schema() for d in docs]

    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("change-*.json"):
        stale.unlink()
    profile_path = out / "profile.json"
    if not profile_path.exists():
        profile_path.write_text(profile_json, encoding="utf-8")
    elif _read_profile(profile_path) != db_profile:
        log.warning(
            "record-seed: kept %s, but it differs from the DB profile the changes were "
            "classified for",
            profile_path,
        )
    write_documents_jsonl(documents, out / "documents.jsonl")
    for name, body in files:
        (out / name).write_text(body, encoding="utf-8")
    return RecordSummary(n_documents=len(docs), n_changes=len(files), n_skipped=n_skipped, out=out)
