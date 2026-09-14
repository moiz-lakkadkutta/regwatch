from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from regwatch.db import DocumentRow
from regwatch.schemas import Applicability, Document, Obligation, RegChange, Todo


def _change() -> RegChange:
    return RegChange(
        document_url="https://x",
        instrument="i",
        jurisdiction="EU",
        summary="s",
        obligations=[Obligation(text="t", quote="the trader shall do X")],
    )


def test_quotes_must_be_in_source() -> None:
    ch = _change()
    assert ch.validate_quotes("… the trader   shall do X …") == []
    assert ch.validate_quotes("nothing here") == ["the trader shall do X"]


def test_todos_must_reuse_obligation_quotes() -> None:
    ch = _change()
    ok = Applicability(
        decision="applies",
        reasons=[],
        todos=[Todo(text="Do X", quote="the trader shall do X", citation_url="https://x")],
    )
    bad = Applicability(
        decision="applies",
        reasons=[],
        todos=[Todo(text="Do Y", quote="invented", citation_url="https://x")],
    )
    assert ok.ungrounded_todos(ch) == []
    assert len(bad.ungrounded_todos(ch)) == 1


def test_document_rejects_domain_outside_allow_list() -> None:
    fetched = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        Document(
            url="https://example.com/page",
            content_hash="abc",
            fetched_at=fetched,
            markdown="# page",
            source_domain="example.com",
        )
    ok = Document(
        url="https://eur-lex.europa.eu/eli/dir/2023/2673/oj",
        content_hash="abc",
        fetched_at=fetched,
        markdown="# page",
        source_domain="eur-lex.europa.eu",
    )
    assert ok.source_domain == "eur-lex.europa.eu"


def test_document_row_round_trip() -> None:
    values = {
        "url": "https://eur-lex.europa.eu/eli/dir/2023/2673/oj",
        "content_hash": "abc",
        "source_domain": "eur-lex.europa.eu",
        "published_date": "2023-11-22",
        "fetched_at": datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        "markdown": "# page",
    }
    assert DocumentRow(**values).to_schema() == Document(**values)
