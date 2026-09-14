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
    id: int | None = Field(default=None, primary_key=True)
    document_url: str = Field(index=True)
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
    model_cost_usd: float = 0.0
    n_search_calls: int = 0
    n_extract_calls: int = 0
    n_docs_new: int = 0
    n_docs_changed: int = 0
    n_docs_unchanged: int = 0


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
