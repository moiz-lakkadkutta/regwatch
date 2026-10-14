"""Grounding for the agent's `decide`, enforced in code (never relaxed in a prompt).

1. Evidence: each quote must be verbatim (whitespace-normalised, as `RegChange.validate_quotes`)
   in the AgentPageRow it names. Callers pass THIS run's pages only. Pages the agent read never
   supply to-do quotes.
2. Reasons: each must cite at least one valid fact (an answered AgentFactRow of this run), one kept
   piece of evidence, or one obligation number of the change, or it is dropped. A kept reason keeps
   only its valid citations.
   An evidence quote shorter than MIN_QUOTE_WORDS (6) words is dropped.
3. `applies` / `does_not` with no reason left, or with no kept reason citing a fact or a piece of
   evidence (obligation numbers alone do not count), → `unclear` with DOWNGRADE_REASON first.
4. To-dos: `ClassifiedPayload` → `enforce()` unchanged. The citation is ours, invented dues are
   nulled, and quotes must be the change's obligation quotes. Rejected to-dos go back to the caller
   for the one retry.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from regwatch.agent.schemas import AgentDecision, CitedReason, Evidence, Fact
from regwatch.agent.tools import PageText
from regwatch.schemas import Applicability, Decision, RegChange, Todo
from regwatch.understand.classify import ClassifiedPayload, enforce
from regwatch.understand.normalize import MIN_QUOTE_WORDS

log = logging.getLogger(__name__)

REASON_CHARS = 300
MAX_REASONS = 4
DOWNGRADE_REASON = "The agent could not back its answer with a source or your answer."


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass
class Grounded:
    applicability: Applicability
    reasons: list[CitedReason]  # parallel to applicability.reasons
    evidence: list[Evidence]  # verified and cited
    rejected: list[Todo]  # to-dos enforce() refused (quote not an obligation quote)
    dropped: list[str] = field(default_factory=list)  # one line per dropped item, logged


def ground_decision(
    decision: AgentDecision,
    change: RegChange,
    pages: Mapping[int, PageText],
    facts: Mapping[int, Fact],
) -> Grounded:
    dropped: list[str] = []
    evidence: dict[int, Evidence] = {}
    for e in decision.evidence:
        page = pages.get(e.page_id)
        quote = _norm(e.quote)
        if e.evidence_id in evidence:
            dropped.append(f"evidence #{e.evidence_id}: duplicate id")
        elif page is None:
            dropped.append(f"evidence #{e.evidence_id}: page #{e.page_id} was not read in this run")
        elif not quote or quote not in _norm(page.markdown):
            dropped.append(f"evidence #{e.evidence_id}: quote is not verbatim in page #{e.page_id}")
        elif len(quote.split()) < MIN_QUOTE_WORDS:
            dropped.append(f"evidence #{e.evidence_id}: quote shorter than {MIN_QUOTE_WORDS} words")
        else:
            evidence[e.evidence_id] = Evidence(
                evidence_id=e.evidence_id, page_id=e.page_id, url=page.url, quote=e.quote.strip()
            )
    n_obligations = len(change.obligations)
    reasons: list[CitedReason] = []
    for i, r in enumerate(decision.reasons, 1):
        cited = CitedReason(
            text=r.text.strip()[:REASON_CHARS],
            fact_ids=sorted({f for f in r.fact_ids if f in facts}),
            evidence_ids=sorted({x for x in r.evidence_ids if x in evidence}),
            obligation_numbers=sorted({n for n in r.obligation_numbers if 1 <= n <= n_obligations}),
        )
        if not cited.text or not (cited.fact_ids or cited.evidence_ids or cited.obligation_numbers):
            dropped.append(f"reason {i}: cites no fact, quote or obligation of this run")
            continue
        reasons.append(cited)
    if len(reasons) > MAX_REASONS:
        dropped.append(f"reasons: kept the first {MAX_REASONS} of {len(reasons)}")
        reasons = reasons[:MAX_REASONS]
    verdict: Decision = decision.decision
    downgrade = CitedReason(
        text=DOWNGRADE_REASON,
        fact_ids=[],
        evidence_ids=[],
        obligation_numbers=[],
        code_written=True,
    )
    if not reasons:
        if verdict != "unclear":
            dropped.append(f"decision: {verdict} downgraded to unclear (no cited reason)")
        verdict = "unclear"
        reasons = [downgrade]
    elif verdict != "unclear" and not any(r.fact_ids or r.evidence_ids for r in reasons):
        # the change's own obligations do not settle what the original check left unclear: a
        # flip needs one of the merchant's answers or a verified page quote
        dropped.append(f"decision: {verdict} downgraded to unclear (no cited fact or page quote)")
        verdict = "unclear"
        reasons = [downgrade, *reasons][:MAX_REASONS]
    payload = ClassifiedPayload(
        decision=verdict, reasons=[r.text for r in reasons], todos=decision.todos
    )
    appl, rejected, _nulled = enforce(payload, change)
    for line in dropped:
        log.warning("agent grounding: %s url=%s", line, change.document_url)
    used = sorted({x for r in reasons for x in r.evidence_ids})
    return Grounded(
        applicability=appl,
        reasons=reasons,
        evidence=[evidence[x] for x in used],
        rejected=rejected,
        dropped=dropped,
    )
