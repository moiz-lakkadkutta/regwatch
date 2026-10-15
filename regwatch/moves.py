"""'This rule moved' banners: an amending act moved a date the merchant may have heard before.

A `RuleMove` is stored only when its quote is a verbatim substring of a stored, allow-listed
source document and that quote states the new date (`RuleMove.validate_quote`), the same rule
`RegChange.validate_quotes()` enforces for obligations. The amending act's name and link must
appear in that document too (EUR-Lex lists them in a consolidated text's "Amended by" table),
so the banner's legal line is grounded as well. Moves are seeded from
`examples/seed-run/rule-moves.json`; seeding is idempotent (one row per `RuleMove.key`).

Two optional parts are grounded the same way, each in its own stored document
(`RuleMove.validate_sources`):
- `amending_text_url`: the amending act's own text, which must state `quote` verbatim too.
- `old_quote`: the earlier text that stated `old_date`, from the act that set it, so "It was …"
  is quoted too (the amending act replaces the article wholesale and never states the old date).
- `exceptions`: products the move leaves out (e.g. EUDR Art. 38(3): "Except as regards the
  products covered by the Annex to Regulation (EU) No 995/2010"). Each names the store categories
  it covers, quotes the product line from that annex and the sentence that keeps their `due`.
  `RuleMove.for_store()` narrows a move to one profile's categories; the banner then says which
  products keep the earlier date and which moved. Matching is by category data, never by store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urldefrag

from pydantic import BaseModel, Field
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from regwatch.config import CATEGORIES
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

_CATEGORY_KEYS = frozenset(k for k, _ in CATEGORIES)
_CATEGORY_NAMES = dict(CATEGORIES)


def spelled(d: date) -> str:
    """'30 June 2027' — how EUR-Lex writes a date in the text of an act."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _norm(text: str) -> str:
    return " ".join(text.split())


MarkdownFor = Callable[[str], str | None]  # stored document url → its markdown, None if absent


def _grounded(quote: str, url: str, markdown_for: MarkdownFor) -> list[str]:
    """[] when `quote` is a verbatim substring of the stored, allow-listed document at `url`."""
    if allowed_domain(url) is None:
        return [f"{url!r} is not on config.SOURCE_DOMAINS"]
    doc_url = urldefrag(url).url
    markdown = markdown_for(doc_url)
    if markdown is None:
        return [f"source {doc_url} is not stored"]
    if _norm(quote) not in _norm(markdown):
        return [f"quote not found verbatim in {doc_url}: {quote[:80]!r}"]
    return []


class MoveException(BaseModel):
    """Products a move leaves out; they keep `due`. Matched to a store by `categories`."""

    categories: list[str] = Field(description="CATEGORIES keys whose products this covers.")
    products: str = Field(description="Plain words, e.g. 'wooden furniture'.")
    act: str = Field(description="Act listing the products, e.g. 'Regulation (EU) No 995/2010'.")
    act_name: str = Field(default="", description="Short name, e.g. 'EU Timber Regulation'.")
    provision: str = Field(description="Where the product list sits, e.g. 'Annex'.")
    quote: str = Field(description="Verbatim product line from `act`.")
    citation_url: str = Field(description="Stored DocumentRow url of `act`, optionally #fragment.")
    due: date = Field(description="The date these products keep.")
    due_provision: str = Field(description="Where `due` is stated, e.g. 'Article 38(2)'.")
    due_quote: str = Field(description="Verbatim sentence stating `due`.")
    due_citation_url: str

    @property
    def source_domain(self) -> str:
        return allowed_domain(self.citation_url) or ""

    @property
    def due_source_domain(self) -> str:
        return allowed_domain(self.due_citation_url) or ""


class RuleMove(BaseModel):
    instrument: str = Field(description="Act whose date moved; prefix of RegChange.instrument.")
    company_size: CompanySize | None = Field(description="Who the new date is for; None = all.")
    audience: str = Field(description="Plain words for company_size, e.g. 'small companies'.")
    old_date: date
    new_date: date
    amending_act: str = Field(description="e.g. 'Regulation (EU) 2025/2650'.")
    amending_url: str = Field(description="Link to the amending act, as the source links it.")
    amending_text_url: str = Field(
        default="", description="Stored DocumentRow url of the amending act's own text."
    )
    provision: str = Field(description="Where the new date sits, e.g. 'Article 38(3)'.")
    quote: str = Field(description="Verbatim sentence from the source stating the new date.")
    citation_url: str = Field(description="Stored DocumentRow url, optionally with a #fragment.")
    caveat: str = Field(default="", description="Plain-language condition the quote states.")
    old_act: str = Field(default="", description="Act that set old_date, e.g. its amending act.")
    old_quote: str = Field(default="", description="Verbatim sentence of old_act stating old_date.")
    old_citation_url: str = Field(default="", description="Stored DocumentRow url of old_act.")
    categories: list[str] = Field(
        default_factory=list, description="CATEGORIES keys the act covers; names what moved."
    )
    exceptions: list[MoveException] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.instrument}|{self.amending_act}|{self.company_size or 'all'}"

    @property
    def source_domain(self) -> str:
        return allowed_domain(self.citation_url) or ""

    @property
    def old_source_domain(self) -> str:
        return allowed_domain(self.old_citation_url) or ""

    @property
    def document_url(self) -> str:
        return urldefrag(self.citation_url).url

    @property
    def source_urls(self) -> list[str]:
        """Every stored document this move is grounded in, the citation document first."""
        urls = [self.citation_url, self.amending_text_url, self.old_citation_url]
        for ex in self.exceptions:
            urls += [ex.citation_url, ex.due_citation_url]
        out: list[str] = []
        for u in urls:
            if u and urldefrag(u).url not in out:
                out.append(urldefrag(u).url)
        return out

    @property
    def moved_names(self) -> list[str]:
        """Plain names of the covered categories no exception keeps back ('leather goods')."""
        kept = {c for ex in self.exceptions for c in ex.categories}
        return [_CATEGORY_NAMES[c].lower() for c in self.categories if c not in kept]

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

    def validate_sources(self, markdown_for: MarkdownFor) -> list[str]:
        """`validate_quote` against the citation document, plus the old-date quote and every
        exception, each against its own stored document. [] = safe to store."""
        main = markdown_for(self.document_url)
        if main is None:
            return [f"source {self.document_url} is not stored"]
        problems = self.validate_quote(main)
        if self.amending_text_url:  # the amending act itself states the new date verbatim
            problems += _grounded(self.quote, self.amending_text_url, markdown_for)
        if bool(self.old_quote) != bool(self.old_citation_url):
            problems.append("old_quote and old_citation_url go together")
        if self.old_quote:
            problems += _grounded(self.old_quote, self.old_citation_url, markdown_for)
            if spelled(self.old_date) not in _norm(self.old_quote):
                problems.append(f"old_quote does not state the old date {spelled(self.old_date)}")
        unknown = set(self.categories) - _CATEGORY_KEYS
        for ex in self.exceptions:
            unknown |= set(ex.categories) - _CATEGORY_KEYS
            if not set(ex.categories) <= set(self.categories):
                problems.append(f"exception {ex.products!r}: categories not in the move's")
            problems += _grounded(ex.quote, ex.citation_url, markdown_for)
            problems += _grounded(ex.due_quote, ex.due_citation_url, markdown_for)
            if spelled(ex.due) not in _norm(ex.due_quote):
                problems.append(f"exception {ex.products!r}: due_quote does not state {ex.due}")
            if not ex.due < self.new_date:
                problems.append(f"exception {ex.products!r}: due must be before new_date")
        if unknown:
            problems.append(f"unknown categories {sorted(unknown)}")
        return problems

    def for_profile(self, profile: Profile) -> bool:
        """A move for one company size is shown to that size, and to 'not sure'."""
        return (
            self.company_size is None
            or profile.company_size is None
            or profile.company_size == self.company_size
        )

    def for_store(self, profile: Profile) -> RuleMove:
        """This move as one store sees it: categories and exceptions narrowed to what the store
        sells. A store that sells none of an exception's products gets no exception."""
        sells = set(profile.categories)
        return self.model_copy(
            update={
                "categories": [c for c in self.categories if c in sells],
                "exceptions": [ex for ex in self.exceptions if sells & set(ex.categories)],
            }
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
    """Store grounded moves; a move whose sources are missing or ungrounded raises ValueError.
    Idempotent: an unchanged move is left alone, a changed one is overwritten. Returns n added."""
    from regwatch.db import DocumentRow  # db does not import this module; keep it that way

    def markdown_for(url: str) -> str | None:
        doc = s.get(DocumentRow, url)
        return None if doc is None else doc.markdown

    added = 0
    for mv in moves:
        problems = mv.validate_sources(markdown_for)
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


def read_grounded_moves(seed_dir: Path) -> list[RuleMove]:
    """`rule-moves.json` checked against the seed's `documents.jsonl` without a DB (for
    `regwatch classify-presets`); raises ValueError on an ungrounded move, as `seed_moves`."""
    from regwatch.jobs.crawl import read_documents_jsonl  # jobs.crawl imports this module

    docs_path = seed_dir / "documents.jsonl"
    docs = (
        {d.url: d.markdown for d in read_documents_jsonl(docs_path)} if docs_path.exists() else {}
    )
    moves = read_moves(seed_dir / "rule-moves.json")
    for mv in moves:
        problems = mv.validate_sources(docs.get)
        if problems:
            raise ValueError(f"rule move {mv.key}: " + "; ".join(problems))
    return moves


def _stored(s: Session) -> list[RuleMove]:
    return [r.to_schema() for r in s.exec(select(RuleMoveRow).order_by(col(RuleMoveRow.key)))]


def moves_for(s: Session, profile: Profile) -> list[RuleMove]:
    """Stored moves for this store's company size, narrowed to what it sells (`for_store`)."""
    return [m.for_store(profile) for m in _stored(s) if m.for_profile(profile)]


def instrument_moves(s: Session, instrument: str) -> list[RuleMove]:
    """Stored moves of the act `instrument` belongs to (prefix match, as `RuleMove.matches`)."""
    return [m for m in _stored(s) if instrument.startswith(m.instrument)]


def matching(
    moves: Iterable[RuleMove], change: RegChange, decision: Decision, dues: Iterable[date]
) -> list[RuleMove]:
    due_list = list(dues)
    return [m for m in moves if m.matches(change, decision, due_list)]
