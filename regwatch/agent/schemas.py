"""What the agent's model returns (`AgentAction`, one strict JSON object per step) and what the
code keeps after grounding (`AgentOutcome`). Nothing here trusts the model: arguments are checked
in agent/tools.py, citations and to-dos in agent/grounding.py."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from regwatch.schemas import Applicability, Decision
from regwatch.understand.classify import TodoDraft, parse_reply
from regwatch.understand.llm_backend import strict_schema

Tool = Literal["search_sources", "read_source", "ask_merchant", "decide"]
SCHEMA_NAME = "agent_action"


class EvidenceDraft(BaseModel):
    """A quote the model says it read on one of this run's pages."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: int
    page_id: int
    quote: str


class ReasonDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    fact_ids: list[int]
    evidence_ids: list[int]
    obligation_numbers: list[int]


class AgentDecision(BaseModel):
    """The `decide` arguments, gathered from an AgentAction."""

    decision: Decision
    reasons: list[ReasonDraft]
    todos: list[TodoDraft]
    evidence: list[EvidenceDraft]


class AgentAction(BaseModel):
    """One step. Strict schema: every field required, fields the tool does not use are null.
    Defaults only make parsing tolerant of a provider that ignores the schema."""

    model_config = ConfigDict(extra="forbid")

    thought: str = ""
    tool: Tool
    query: str | None = None
    domain: str | None = None
    url: str | None = None
    question: str | None = None
    options: list[str] | None = None
    decision: Decision | None = None
    reasons: list[ReasonDraft] | None = None
    todos: list[TodoDraft] | None = None
    evidence: list[EvidenceDraft] | None = None

    def as_decision(self) -> AgentDecision | None:
        """The decide arguments; None when `decision` is missing."""
        if self.decision is None:
            return None
        return AgentDecision(
            decision=self.decision,
            reasons=self.reasons or [],
            todos=self.todos or [],
            evidence=self.evidence or [],
        )

    def args(self) -> dict[str, Any]:
        """The non-null tool arguments, as stored in AgentStepRow.args_json."""
        return self.model_dump(mode="json", exclude={"thought", "tool"}, exclude_none=True)


def action_schema(*, final: bool = False) -> dict[str, Any]:
    """Strict AgentAction schema; `final` allows only "decide" (the run's last step)."""
    schema = strict_schema(AgentAction)
    if final:
        schema = copy.deepcopy(schema)
        schema["properties"]["tool"] = {"type": "string", "enum": ["decide"]}
    return schema


def response_format(*, final: bool = False) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": action_schema(final=final), "strict": True},
    }


def parse_action(text: str) -> AgentAction:
    """Same stripping as classify; raises PayloadError."""
    return parse_reply(text, AgentAction)


class Evidence(BaseModel):
    """A verified quote: verbatim (whitespace-normalised) in AgentPageRow `page_id` at `url`."""

    evidence_id: int
    page_id: int
    url: str
    quote: str


class Fact(BaseModel):
    """An answered ask_merchant question; `fact_id` is the AgentFactRow id."""

    fact_id: int
    question: str
    answer: str


class CitedReason(BaseModel):
    """A kept reason with only its valid citations; `code_written` marks DOWNGRADE_REASON."""

    text: str
    fact_ids: list[int]
    evidence_ids: list[int]
    obligation_numbers: list[int]
    code_written: bool = False


class AgentOutcome(BaseModel):
    """Stored in AgentRunRow.outcome_json once a run is done; rendered only to its visitor.
    `reasons` is parallel to `applicability.reasons`."""

    applicability: Applicability
    reasons: list[CitedReason]
    evidence: list[Evidence]
    facts: list[Fact]
    dropped: list[str]
