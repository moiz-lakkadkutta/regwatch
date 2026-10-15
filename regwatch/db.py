"""SQLite via SQLModel. Tables mirror schemas.py; JSON columns hold the nested lists."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import inspect, text
from sqlmodel import Field, Session, SQLModel, col, create_engine, func, select
from sqlmodel.sql.expression import Select

from regwatch.config import SEED_REGULATIONS, Settings
from regwatch.schemas import Applicability, Document, Profile, RegChange


class ProfileRow(SQLModel, table=True):
    """A store profile. `slug` is set on the example stores `regwatch seed` loads from
    examples/seed-run/stores (the visitor's switcher); a profile saved on /profile has none."""

    id: int | None = Field(default=None, primary_key=True)
    slug: str | None = Field(default=None, index=True)
    name: str | None = None
    blurb: str | None = None
    establishment_country: str
    ship_to: str  # JSON list
    categories: str  # JSON list
    platform: str = "shopify"
    company_size: str | None = None
    supply: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_schema(cls, profile: Profile) -> ProfileRow:
        return cls(
            establishment_country=profile.establishment_country,
            ship_to=json.dumps(profile.ship_to),
            categories=json.dumps(profile.categories),
            platform=profile.platform,
            company_size=profile.company_size,
            supply=profile.supply,
        )

    def to_schema(self) -> Profile:
        return Profile.model_validate(
            {
                "establishment_country": self.establishment_country,
                "ship_to": json.loads(self.ship_to),
                "categories": json.loads(self.categories),
                "platform": self.platform,
                "company_size": self.company_size,
                "supply": self.supply,
            }
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


def utcnow() -> datetime:
    """Naive UTC, the form SQLite hands back; agent rows are stored and compared in it."""
    return datetime.now(UTC).replace(tzinfo=None)


AGENT_ACTIVE: tuple[str, ...] = ("queued", "running", "waiting")


class AgentRunRow(SQLModel, table=True):
    """One "Let the agent check" run: one visitor, one (profile, change). The agent never writes
    DocumentRow, RegChangeRow or ApplicabilityRow; its grounded outcome lives in `outcome_json`
    and is shown only to `visitor_id`."""

    id: int | None = Field(default=None, primary_key=True)
    visitor_id: str = Field(index=True)
    profile_id: int = Field(index=True)
    regchange_id: int = Field(index=True)
    status: str = "queued"  # queued|running|waiting|done|stopped|failed|expired
    stop_reason: str = ""
    decision: str | None = None
    outcome_json: str = ""  # agent.schemas.AgentOutcome once done
    n_steps: int = 0
    tavily_credits: int = 0
    cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AgentStepRow(SQLModel, table=True):
    """One model call of a run and the one tool it chose; `result_json` is what the model sees
    next (already truncated). The day's agent spend is the sum over these rows."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    idx: int  # 1-based
    tool: str  # a Tool, or "invalid" for an unparseable reply
    thought: str = ""  # ≤ 300 chars
    args_json: str = "{}"
    result_json: str = "{}"
    credits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow, index=True)


class AgentPageRow(SQLModel, table=True):
    """A page the agent read, full markdown for evidence checks. Never a DocumentRow: that table
    is keyed by URL and a re-read would overwrite the seed's content hash."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    url: str
    source_domain: str
    markdown: str
    fetched_at: datetime = Field(default_factory=utcnow)


class AgentFactRow(SQLModel, table=True):
    """An ask_merchant question and the option the visitor picked (by index; never free text)."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    step_idx: int
    question: str
    options_json: str  # JSON list[str]
    chosen_index: int | None = None
    chosen_text: str = ""


# Columns added after the first deploy: `create_all` never alters an existing table, so a DB
# made before them gets them here (nullable, no default — what the model declares).
_ADDED_COLUMNS: dict[str, tuple[str, ...]] = {"profilerow": ("slug", "name", "blurb")}


def _add_missing_columns(engine) -> None:  # type: ignore[no-untyped-def]
    insp = inspect(engine)
    for table, columns in _ADDED_COLUMNS.items():
        if not insp.has_table(table):
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        missing = [c for c in columns if c not in have]
        if not missing:
            continue
        with engine.begin() as conn:
            for column in missing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR"))


def make_engine(settings: Settings):  # type: ignore[no-untyped-def]
    connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}
    engine = create_engine(settings.db_url, connect_args=connect_args)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


@contextmanager
def session(engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    with Session(engine) as s:
        yield s


def store_profiles(s: Session) -> list[ProfileRow]:
    """The example stores (rows with a slug), the default store first (seed loads it first)."""
    return list(
        s.exec(
            select(ProfileRow).where(col(ProfileRow.slug).is_not(None)).order_by(col(ProfileRow.id))
        ).all()
    )


def current_profile(s: Session, store: str | None = None) -> ProfileRow | None:
    """The profile a request is shown: the example store `store` (the visitor's switcher cookie)
    when it exists; else the newest profile saved on /profile; else the default example store
    (the first one seeded). An unknown `store` falls through, so a stale cookie never 404s."""
    if store:
        chosen = s.exec(select(ProfileRow).where(ProfileRow.slug == store)).first()
        if chosen is not None:
            return chosen
    saved = s.exec(
        select(ProfileRow).where(col(ProfileRow.slug).is_(None)).order_by(col(ProfileRow.id).desc())
    ).first()
    if saved is not None:
        return saved
    return s.exec(select(ProfileRow).order_by(col(ProfileRow.id))).first()


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


@dataclass(frozen=True)
class FeedRow:
    """One Applicability joined to its change and (outer) source document, as stored."""

    change_id: int
    change: RegChange
    appl: Applicability
    created_at: datetime  # naive, UTC as stored
    tag: str | None  # regulation_tag_for(doc)
    source_domain: str  # doc.source_domain, else the document_url host
    published_date: str | None
    fetched_at: datetime | None
    content_hash: str
    normalize_model: str
    classify_model: str
    cost_usd: float
    tavily_credits: int


def regulation_tag_for(doc: DocumentRow | None) -> str | None:
    """First tag in SEED_REGULATIONS order present on `doc`; None without a doc or a known tag."""
    if doc is None:
        return None
    tags: list[str] = json.loads(doc.regulation_tags)
    for r in SEED_REGULATIONS:
        if r["tag"] in tags:
            return r["tag"]
    return None


def source_domain_for(document_url: str, doc: DocumentRow | None) -> str:
    """The stored source domain, else the host of `document_url` ("" if it has none)."""
    if doc is not None:
        return doc.source_domain
    return urlparse(document_url).hostname or ""


def _feed_row(app_row: ApplicabilityRow, ch_row: RegChangeRow, doc: DocumentRow | None) -> FeedRow:
    assert ch_row.id is not None
    return FeedRow(
        change_id=ch_row.id,
        change=ch_row.to_schema(),
        appl=app_row.to_schema(),
        created_at=ch_row.created_at,
        tag=regulation_tag_for(doc),
        source_domain=source_domain_for(ch_row.document_url, doc),
        published_date=doc.published_date if doc is not None else None,
        fetched_at=doc.fetched_at if doc is not None else None,
        content_hash=ch_row.content_hash,
        normalize_model=ch_row.model,
        classify_model=app_row.model,
        cost_usd=app_row.cost_usd,
        tavily_credits=app_row.tavily_credits,
    )


def _feed_select() -> Select[tuple[ApplicabilityRow, RegChangeRow, DocumentRow]]:
    """Applicability → its change → (outer) the source document, joined by url."""
    return (
        select(ApplicabilityRow, RegChangeRow, DocumentRow)
        .join(RegChangeRow, col(ApplicabilityRow.regchange_id) == col(RegChangeRow.id))
        .outerjoin(DocumentRow, col(DocumentRow.url) == col(RegChangeRow.document_url))
    )


def feed_rows(s: Session, profile_id: int) -> list[FeedRow]:
    """Every classified change for `profile_id`, newest change first."""
    stmt = (
        _feed_select()
        .where(ApplicabilityRow.profile_id == profile_id)
        .order_by(col(RegChangeRow.created_at).desc(), col(RegChangeRow.id).desc())
    )
    return [_feed_row(a, c, d) for a, c, d in s.exec(stmt).all()]


def feed_row(s: Session, profile_id: int, change_id: int) -> FeedRow | None:
    """One classified change for `profile_id`, or None when not (yet) classified."""
    stmt = _feed_select().where(
        ApplicabilityRow.profile_id == profile_id, RegChangeRow.id == change_id
    )
    hit = s.exec(stmt).first()
    if hit is None:
        return None
    a, c, d = hit
    return _feed_row(a, c, d)


def unchecked_changes(s: Session, profile_id: int) -> list[RegChangeRow]:
    """RegChangeRows with no ApplicabilityRow for `profile_id`, newest first."""
    done = select(ApplicabilityRow.id).where(
        ApplicabilityRow.regchange_id == RegChangeRow.id,
        ApplicabilityRow.profile_id == profile_id,
    )
    stmt = (
        select(RegChangeRow)
        .where(~done.exists())
        .order_by(col(RegChangeRow.created_at).desc(), col(RegChangeRow.id).desc())
    )
    return list(s.exec(stmt).all())


def corpus_counts(s: Session) -> tuple[int, int]:
    """(number of stored documents, number of distinct source domains)."""
    n_docs = s.exec(select(func.count(col(DocumentRow.url)))).one()
    n_sources = s.exec(select(func.count(func.distinct(col(DocumentRow.source_domain))))).one()
    return int(n_docs), int(n_sources)
