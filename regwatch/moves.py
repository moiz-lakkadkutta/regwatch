"""'This rule moved' banners: an amending act moved a date the merchant may have heard before.

A `RuleMove` is stored only when its quote is a verbatim substring of a stored, allow-listed
source document and that quote states the new date (`RuleMove.validate_quote`), the same rule
`RegChange.validate_quotes()` enforces for obligations. The amending act's name and link must
appear in that document too (EUR-Lex lists them in a consolidated text's "Amended by" table),
so the banner's legal line is grounded as well. Moves are seeded from
`examples/seed-run/rule-moves.json`; seeding is idempotent (one row per `RuleMove.key`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urldefrag

from pydantic import BaseModel, Field
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from regwatch.ingest.tavily_backend import allowed_domain
from regwatch.schemas import CompanySize, Decision, Profile, RegChange

log = logging.getLogger(__name__)

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def spelled(d: date) -> str:
    """'30 June 2027' — how EUR-Lex writes a date in the text of an act."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _norm(text: str) -> str:
    return " ".join(text.split())


class RuleMove(BaseModel):
    instrument: str = Field(description="Act whose date moved; prefix of RegChange.instrument.")
    company_size: CompanySize | None = Field(description="Who the new date is for; None = all.")
    audience: str = Field(description="Plain words for company_size, e.g. 'small companies'.")
    old_date: date
    new_date: date
    amending_act: str = Field(description="e.g. 'Regulation (EU) 2025/2650'.")
    amending_url: str = Field(description="Link to the amending act, as the source links it.")
    provision: str = Field(description="Where the new date sits, e.g. 'Article 38(3)'.")
    quote: str = Field(description="Verbatim sentence from the source stating the new date.")
    citation_url: str = Field(description="Stored DocumentRow url, optionally with a #fragment.")
    caveat: str = Field(default="", description="Plain-language exception the quote states.")

    @property
    def key(self) -> str:
        return f"{self.instrument}|{self.amending_act}|{self.company_size or 'all'}"

    @property
    def source_domain(self) -> str:
        return allowed_domain(self.citation_url) or ""

    @property
    def document_url(self) -> str:
        return urldefrag(self.citation_url).url

    def validate_quote(self, source_markdown: str) -> list[str]:
        """Return what is NOT grounded in the source ([] = safe to store)."""
        norm = _norm(source_markdown)
        problems: list[str] = []
        if _norm(self.quote) not in norm:
            problems.append(f"quote not found verbatim: {self.quote[:80]!r}")
        if spelled(self.new_date) not in _norm(self.quote):
            problems.append(f"quote does not state the new date {spelled(self.new_date)}")
        if self.amending_act.upper() not in norm.upper():
            problems.append(f"source does not name the amending act {self.amending_act!r}")
        if self.amending_url not in source_markdown:
            problems.append(f"source does not link the amending act {self.amending_url!r}")
        if not self.old_date < self.new_date:
            problems.append("old_date must be before new_date")
        for url in (self.citation_url, self.amending_url):
            if allowed_domain(url) is None:
                problems.append(f"{url!r} is not on config.SOURCE_DOMAINS")
        return problems

    def for_profile(self, profile: Profile) -> bool:
        """A move for one company size is shown to that size, and to 'not sure'."""
        return (
            self.company_size is None
            or profile.company_size is None
            or profile.company_size == self.company_size
        )

    def matches(self, change: RegChange, decision: Decision, dues: Iterable[date]) -> bool:
        """The change is part of the moved act, matters to the merchant, and carries the new
        date (as an effective date or a to-do due date)."""
        if decision == "does_not" or not change.instrument.startswith(self.instrument):
            return False
        return self.new_date in change.effective_dates or self.new_date in set(dues)


class RuleMoveRow(SQLModel, table=True):
    key: str = SQLField(primary_key=True)
    document_url: str = SQLField(index=True)
    payload: str  # RuleMove JSON
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))

    def to_schema(self) -> RuleMove:
        return RuleMove.model_validate_json(self.payload)


def read_moves(path: Path) -> list[RuleMove]:
    if not path.exists():
        return []
    return [RuleMove.model_validate(m) for m in json.loads(path.read_text(encoding="utf-8"))]


def seed_moves(s: Session, moves: Iterable[RuleMove]) -> int:
    """Store grounded moves; a move whose source is missing or ungrounded raises ValueError.
    Idempotent: an unchanged move is left alone, a changed one is overwritten. Returns n added."""
    from regwatch.db import DocumentRow  # db does not import this module; keep it that way

    added = 0
    for mv in moves:
        doc = s.get(DocumentRow, mv.document_url)
        if doc is None:
            raise ValueError(f"rule move {mv.key}: source {mv.document_url} is not stored")
        problems = mv.validate_quote(doc.markdown)
        if problems:
            raise ValueError(f"rule move {mv.key}: " + "; ".join(problems))
        payload = mv.model_dump_json()
        row = s.get(RuleMoveRow, mv.key)
        if row is None:
            s.add(RuleMoveRow(key=mv.key, document_url=mv.document_url, payload=payload))
            added += 1
        elif row.payload != payload:
            row.payload = payload
            row.document_url = mv.document_url
            s.add(row)
    s.commit()
    return added


def moves_for(s: Session, profile: Profile) -> list[RuleMove]:
    rows = s.exec(select(RuleMoveRow).order_by(col(RuleMoveRow.key))).all()
    return [m for m in (r.to_schema() for r in rows) if m.for_profile(profile)]


def matching(
    moves: Iterable[RuleMove], change: RegChange, decision: Decision, dues: Iterable[date]
) -> list[RuleMove]:
    due_list = list(dues)
    return [m for m in moves if m.matches(change, decision, due_list)]
