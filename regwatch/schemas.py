"""Contracts between ingest → understand → app.

Grounding rule enforced in code: every `Obligation.quote` must be a verbatim substring of the source
document, and every `Todo.quote` must equal one of the change's obligation quotes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from regwatch.config import SOURCE_DOMAINS

Decision = Literal["applies", "does_not", "unclear"]


class Profile(BaseModel):
    establishment_country: str = Field(min_length=2, max_length=2)
    ship_to: list[str]
    categories: list[str]
    platform: str = "shopify"


class Document(BaseModel):
    """One extracted page. content_hash = ingest.diff.content_hash(markdown); computed by the caller."""

    url: str
    content_hash: str
    published_date: str | None = None  # as Tavily returns it; str mirrors DocumentRow
    fetched_at: datetime
    markdown: str
    source_domain: str

    @field_validator("source_domain")
    @classmethod
    def _allow_listed(cls, v: str) -> str:
        if v not in SOURCE_DOMAINS:
            raise ValueError(f"{v!r} is not in config.SOURCE_DOMAINS")
        return v


class Obligation(BaseModel):
    text: str = Field(description="Plain-language obligation.")
    quote: str = Field(description="Verbatim sentence from the source.")


class RegChange(BaseModel):
    document_url: str
    instrument: str
    jurisdiction: str
    summary: str
    effective_dates: list[date] = Field(default_factory=list)
    obligations: list[Obligation]

    def validate_quotes(self, source_markdown: str) -> list[str]:
        """Return the quotes that are NOT found verbatim in the source."""
        norm = " ".join(source_markdown.split())
        return [o.quote for o in self.obligations if " ".join(o.quote.split()) not in norm]


class Todo(BaseModel):
    text: str = Field(
        description="Verb phrase with an object, e.g. 'Add the two-step withdrawal button to checkout'."
    )
    due: date | None = None
    quote: str
    citation_url: str


class Applicability(BaseModel):
    decision: Decision
    reasons: list[str] = Field(
        description="Reference profile fields: ship-to, categories, establishment."
    )
    todos: list[Todo]

    def ungrounded_todos(self, change: RegChange) -> list[Todo]:
        quotes = {" ".join(o.quote.split()) for o in change.obligations}
        return [t for t in self.todos if " ".join(t.quote.split()) not in quotes]
