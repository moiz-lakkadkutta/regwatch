"""SQLite via SQLModel. Tables mirror schemas.py; JSON columns hold the nested lists."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from regwatch.config import Settings
from regwatch.schemas import Applicability, Document, Profile, RegChange


class ProfileRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    establishment_country: str
    ship_to: str  # JSON list
    categories: str  # JSON list
    platform: str = "shopify"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_schema(self) -> Profile:
        return Profile(
            establishment_country=self.establishment_country,
            ship_to=json.loads(self.ship_to),
            categories=json.loads(self.categories),
            platform=self.platform,
        )


class DocumentRow(SQLModel, table=True):
    url: str = Field(primary_key=True)
    content_hash: str
    source_domain: str
    published_date: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    markdown: str
    regulation_tags: str = "[]"  # JSON list

    def to_schema(self) -> Document:
        tags: list[str] = json.loads(self.regulation_tags)
        return Document(
            url=self.url,
            content_hash=self.content_hash,
            published_date=self.published_date,
            fetched_at=self.fetched_at,
            markdown=self.markdown,
            source_domain=self.source_domain,
            regulation_tags=tags,
        )


class RegChangeRow(SQLModel, table=True):
    """Normalisation idempotency key: (document_url, content_hash) of the source DocumentRow."""

    id: int | None = Field(default=None, primary_key=True)
    document_url: str = Field(index=True)
    content_hash: str = Field(default="", index=True)
    model: str = ""
    instrument: str
    jurisdiction: str
    summary: str
    effective_dates: str  # JSON list of ISO dates
    obligations: str  # JSON
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_schema(self) -> RegChange:
        return RegChange.model_validate(
            {
                "document_url": self.document_url,
                "instrument": self.instrument,
                "jurisdiction": self.jurisdiction,
                "summary": self.summary,
                "effective_dates": json.loads(self.effective_dates),
                "obligations": json.loads(self.obligations),
            }
        )


class ApplicabilityRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    profile_id: int = Field(index=True)
    regchange_id: int = Field(index=True)
    decision: str
    reasons: str  # JSON
    todos: str  # JSON
    model: str
    cost_usd: float = 0.0
    tavily_credits: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_schema(self) -> Applicability:
        return Applicability.model_validate(
            {
                "decision": self.decision,
                "reasons": json.loads(self.reasons),
                "todos": json.loads(self.todos),
            }
        )


class CrawlRunRow(SQLModel, table=True):
    """One crawl job run; `tavily_credits` is what the feed footer shows as credits per crawl."""

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime
    finished_at: datetime | None = None
    stage: str
    tavily_credits: int = 0
    model_cost_usd: float = 0.0  # every model call of the run (normalize + classify)
    classify_cost_usd: float = 0.0  # the classify share, so per-stage lines can be rebuilt
    n_search_calls: int = 0
    n_extract_calls: int = 0
    n_docs_new: int = 0
    n_docs_changed: int = 0
    n_docs_unchanged: int = 0
    n_changes_new: int = 0
    n_normalize_retried: int = 0
    n_normalize_failed: int = 0
    n_classified: int = 0
    n_applies: int = 0
    n_does_not: int = 0
    n_unclear: int = 0
    n_classify_retried: int = 0
    n_classify_failed: int = 0
    n_todos: int = 0
    n_todos_dropped: int = 0


class TodoStateRow(SQLModel, table=True):
    """Per-profile 'done' marks for to-dos, keyed by the to-do's quote hash."""

    id: int | None = Field(default=None, primary_key=True)
    profile_id: int = Field(index=True)
    todo_key: str = Field(index=True)
    done: bool = True


def make_engine(settings: Settings):  # type: ignore[no-untyped-def]
    connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}
    engine = create_engine(settings.db_url, connect_args=connect_args)
    SQLModel.metadata.create_all(engine)
    return engine


@contextmanager
def session(engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    with Session(engine) as s:
        yield s


def current_profile(s: Session) -> ProfileRow | None:
    return s.exec(select(ProfileRow).order_by(col(ProfileRow.id).desc())).first()


def last_crawl(s: Session) -> CrawlRunRow | None:
    return s.exec(select(CrawlRunRow).order_by(col(CrawlRunRow.id).desc())).first()


def documents_needing_normalize(s: Session) -> list[DocumentRow]:
    """DocumentRows with no RegChangeRow for their (url, content_hash), sorted by url."""
    done = select(RegChangeRow.id).where(
        RegChangeRow.document_url == DocumentRow.url,
        RegChangeRow.content_hash == DocumentRow.content_hash,
    )
    return list(s.exec(select(DocumentRow).where(~done.exists()).order_by(DocumentRow.url)).all())


def store_regchange(
    s: Session, change: RegChange, *, content_hash: str, model: str
) -> RegChangeRow:
    """Persist a grounded change. Callers must have checked `change.validate_quotes()` == []."""
    row = RegChangeRow(
        document_url=change.document_url,
        content_hash=content_hash,
        model=model,
        instrument=change.instrument,
        jurisdiction=change.jurisdiction,
        summary=change.summary,
        effective_dates=json.dumps([d.isoformat() for d in change.effective_dates]),
        obligations=json.dumps([o.model_dump() for o in change.obligations]),
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def regchanges_needing_classify(s: Session, profile_id: int) -> list[RegChangeRow]:
    """RegChangeRows with no ApplicabilityRow for `profile_id`, ordered by id."""
    done = select(ApplicabilityRow.id).where(
        ApplicabilityRow.regchange_id == RegChangeRow.id,
        ApplicabilityRow.profile_id == profile_id,
    )
    return list(
        s.exec(select(RegChangeRow).where(~done.exists()).order_by(col(RegChangeRow.id))).all()
    )


def store_applicability(
    s: Session,
    *,
    profile_id: int,
    regchange_id: int,
    appl: Applicability,
    model: str,
    cost_usd: float,
) -> ApplicabilityRow:
    """Persist a classification. Callers must have checked `appl.ungrounded_todos(change)` == []."""
    row = ApplicabilityRow(
        profile_id=profile_id,
        regchange_id=regchange_id,
        decision=appl.decision,
        reasons=json.dumps(appl.reasons),
        todos=json.dumps([t.model_dump(mode="json") for t in appl.todos]),
        model=model,
        cost_usd=cost_usd,
        tavily_credits=0,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row
