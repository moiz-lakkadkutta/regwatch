from regwatch.schemas import Applicability, Obligation, RegChange, Todo


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
