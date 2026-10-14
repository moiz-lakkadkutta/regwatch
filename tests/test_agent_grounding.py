import logging
from datetime import date
from typing import Any

import pytest

from regwatch.agent.grounding import DOWNGRADE_REASON, MAX_REASONS, REASON_CHARS, ground_decision
from regwatch.agent.schemas import AgentDecision, EvidenceDraft, Fact, ReasonDraft
from regwatch.agent.tools import PageText
from regwatch.understand.classify import TodoDraft
from tests.conftest import eudr_change
from tests.fakes import IHK_PAGE, IHK_URL, TRADER_QUOTE

CHANGE = eudr_change()
DUE_DILIGENCE = CHANGE.obligations[3].quote
PAGES = {1: PageText(url=IHK_URL, source_domain="ihk.de", markdown=IHK_PAGE)}
FACTS = {
    1: Fact(
        fact_id=1,
        question="Do you import wood or leather goods from outside the EU yourself?",
        answer="No, I buy only from EU suppliers",
    )
}


def _reason(
    text: str = "You buy only from EU suppliers.",
    *,
    facts: tuple[int, ...] = (),
    evidence: tuple[int, ...] = (),
    obligations: tuple[int, ...] = (),
) -> ReasonDraft:
    return ReasonDraft(
        text=text,
        fact_ids=list(facts),
        evidence_ids=list(evidence),
        obligation_numbers=list(obligations),
    )


def _decide(
    decision: Any,
    reasons: list[ReasonDraft],
    *,
    todos: list[TodoDraft] | None = None,
    evidence: list[EvidenceDraft] | None = None,
) -> AgentDecision:
    return AgentDecision(
        decision=decision, reasons=reasons, todos=todos or [], evidence=evidence or []
    )


def test_verbatim_evidence_is_kept_whitespace_normalised() -> None:
    spaced = TRADER_QUOTE.replace(" ", "\n  ", 2)
    g = ground_decision(
        _decide(
            "does_not",
            [_reason(evidence=(1,))],
            evidence=[EvidenceDraft(evidence_id=1, page_id=1, quote=spaced)],
        ),
        CHANGE,
        PAGES,
        FACTS,
    )
    assert g.applicability.decision == "does_not"
    assert [e.url for e in g.evidence] == [IHK_URL] and g.reasons[0].evidence_ids == [1]


def test_evidence_not_verbatim_or_from_another_page_dropped() -> None:
    ev = [
        EvidenceDraft(evidence_id=1, page_id=1, quote="Traders are never operators."),
        EvidenceDraft(evidence_id=2, page_id=9, quote=TRADER_QUOTE),  # page of another run
        EvidenceDraft(evidence_id=3, page_id=1, quote="   "),
    ]
    g = ground_decision(
        _decide("does_not", [_reason(evidence=(1, 2, 3))], evidence=ev), CHANGE, PAGES, FACTS
    )
    assert g.evidence == []
    assert g.applicability.decision == "unclear"
    assert g.applicability.reasons == [DOWNGRADE_REASON] and g.reasons[0].code_written
    assert any("not verbatim" in d for d in g.dropped)
    assert any("page #9" in d for d in g.dropped)


def test_reason_citing_nothing_valid_dropped_and_citations_filtered() -> None:
    reasons = [
        _reason("No citation at all."),
        _reason("Unknown ids.", facts=(99,), evidence=(7,), obligations=(0, 8)),
        _reason("Kept.", facts=(1, 99), obligations=(4, 8)),
    ]
    g = ground_decision(_decide("does_not", reasons), CHANGE, PAGES, FACTS)
    assert g.applicability.reasons == ["Kept."]
    assert (g.reasons[0].fact_ids, g.reasons[0].obligation_numbers) == ([1], [4])
    assert len([d for d in g.dropped if d.startswith("reason")]) == 2


def test_applies_without_a_valid_citation_becomes_unclear() -> None:
    todo = TodoDraft(
        text="Check each product before you sell it", due="2026-12-30", quote=DUE_DILIGENCE
    )
    g = ground_decision(
        _decide("applies", [_reason(facts=(42,))], todos=[todo, todo]), CHANGE, PAGES, FACTS
    )
    assert g.applicability.decision == "unclear"
    assert g.applicability.reasons == [DOWNGRADE_REASON]
    assert len(g.applicability.todos) == 1  # enforce(): unclear keeps at most one to-do
    assert any("downgraded to unclear" in d for d in g.dropped)


def test_todos_go_through_enforce_and_page_quotes_are_never_todos() -> None:
    todos = [
        TodoDraft(
            text="Check each product before you sell it", due="2026-12-30", quote=DUE_DILIGENCE
        ),
        TodoDraft(
            text="Keep the IHK note", due=None, quote=TRADER_QUOTE
        ),  # verbatim on page, not an obligation
        TodoDraft(
            text="Collect supplier data in July",
            due="2026-07-01",
            quote=CHANGE.obligations[4].quote,
        ),
    ]
    g = ground_decision(
        _decide("applies", [_reason(facts=(1,), obligations=(4,))], todos=todos),
        CHANGE,
        PAGES,
        FACTS,
    )
    assert g.applicability.decision == "applies"
    assert [t.quote for t in g.applicability.todos] == [DUE_DILIGENCE, CHANGE.obligations[4].quote]
    assert g.applicability.todos[0].due == date(2026, 12, 30)
    assert g.applicability.todos[1].due is None  # invented date nulled by enforce()
    assert all(t.citation_url == CHANGE.document_url for t in g.applicability.todos)
    assert [t.quote for t in g.rejected] == [TRADER_QUOTE]
    assert g.applicability.ungrounded_todos(CHANGE) == []


def test_does_not_keeps_no_todos() -> None:
    todo = TodoDraft(text="Check each product", due=None, quote=DUE_DILIGENCE)
    g = ground_decision(
        _decide("does_not", [_reason(facts=(1,))], todos=[todo]), CHANGE, PAGES, FACTS
    )
    assert g.applicability.decision == "does_not" and g.applicability.todos == []


def test_reason_text_and_count_are_capped() -> None:
    reasons = [
        _reason("a" * 400, facts=(1,), obligations=(1,)),
        *[_reason(f"r{i}", obligations=(1,)) for i in range(5)],
    ]
    g = ground_decision(_decide("applies", reasons), CHANGE, PAGES, FACTS)
    assert len(g.applicability.reasons) == MAX_REASONS
    assert len(g.applicability.reasons[0]) == REASON_CHARS


def test_every_drop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="regwatch.agent.grounding"):
        ground_decision(_decide("applies", [_reason("x")]), CHANGE, PAGES, FACTS)
    assert "cites no fact, quote or obligation" in caplog.text
    assert "downgraded to unclear" in caplog.text


def test_evidence_quote_shorter_than_six_words_is_dropped() -> None:
    short = TRADER_QUOTE.split()[:5]
    assert " ".join(short) in IHK_PAGE
    g = ground_decision(
        _decide(
            "does_not",
            [_reason(evidence=(1,))],
            evidence=[EvidenceDraft(evidence_id=1, page_id=1, quote="  ".join(short))],
        ),
        CHANGE,
        PAGES,
        FACTS,
    )
    assert g.evidence == [] and g.applicability.decision == "unclear"
    assert "evidence #1: quote shorter than 6 words" in g.dropped


def test_applies_or_does_not_backed_only_by_obligations_becomes_unclear() -> None:
    g = ground_decision(
        _decide("does_not", [_reason("Obligation 4 is for operators.", obligations=(4,))]),
        CHANGE,
        PAGES,
        FACTS,
    )
    assert g.applicability.decision == "unclear"
    assert g.applicability.reasons == [DOWNGRADE_REASON, "Obligation 4 is for operators."]
    assert any("no cited fact or page quote" in d for d in g.dropped)


def test_an_obligation_reason_is_kept_beside_one_citing_a_fact() -> None:
    reasons = [
        _reason("Obligation 4 is for operators.", obligations=(4,)),
        _reason("You buy only from EU suppliers.", facts=(1,)),
    ]
    g = ground_decision(_decide("does_not", reasons), CHANGE, PAGES, FACTS)
    assert g.applicability.decision == "does_not"
    assert g.applicability.reasons == [
        "Obligation 4 is for operators.",
        "You buy only from EU suppliers.",
    ]


def test_unclear_backed_only_by_obligations_stays_as_is() -> None:
    g = ground_decision(
        _decide("unclear", [_reason("Depends on obligation 1.", obligations=(1,))]),
        CHANGE,
        PAGES,
        FACTS,
    )
    assert g.applicability.decision == "unclear"
    assert g.applicability.reasons == ["Depends on obligation 1."]
