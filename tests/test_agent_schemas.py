import json
import threading
from typing import Any

import pytest
from sqlalchemy import inspect

from regwatch.agent.schemas import AgentAction, action_schema, parse_action, response_format
from regwatch.config import Settings
from regwatch.db import make_engine
from regwatch.understand.normalize import PayloadError

AGENT_ENV = (
    "REGWATCH_AGENT_ENABLED",
    "REGWATCH_AGENT_DAILY_USD",
    "REGWATCH_AGENT_DAILY_CREDITS",
    "REGWATCH_AGENT_RUN_STEPS",
    "REGWATCH_AGENT_RUN_CREDITS",
    "REGWATCH_AGENT_VISITOR_RUNS_PER_HOUR",
    "REGWATCH_PROFILE_LOCKED",
)
FIELDS = {
    "thought",
    "tool",
    "query",
    "domain",
    "url",
    "question",
    "options",
    "decision",
    "reasons",
    "todos",
    "evidence",
}


def test_agent_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in AGENT_ENV:
        monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.agent_enabled is False and s.profile_locked is False
    assert (s.agent_daily_usd, s.agent_daily_credits) == (1.0, 60)
    assert (s.agent_run_steps, s.agent_run_credits, s.agent_visitor_runs_per_hour) == (6, 10, 5)


def test_agent_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGWATCH_AGENT_ENABLED", "true")
    monkeypatch.setenv("REGWATCH_PROFILE_LOCKED", "true")
    monkeypatch.setenv("REGWATCH_AGENT_DAILY_USD", "0.25")
    s = Settings()
    assert s.agent_enabled and s.profile_locked and s.agent_daily_usd == 0.25


def test_agent_tables_are_created(tmp_settings: Settings) -> None:
    names = set(inspect(make_engine(tmp_settings)).get_table_names())
    assert {"agentrunrow", "agentsteprow", "agentpagerow", "agentfactrow"} <= names


def test_action_schema_is_strict() -> None:
    schema = action_schema()
    assert set(schema["properties"]) == FIELDS
    assert schema["required"] == list(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["tool"]["enum"] == [
        "search_sources",
        "read_source",
        "ask_merchant",
        "decide",
    ]
    defs: dict[str, Any] = schema["$defs"]
    assert set(defs) == {"EvidenceDraft", "ReasonDraft", "TodoDraft"}
    for d in defs.values():
        assert d["required"] == list(d["properties"]) and d["additionalProperties"] is False
    dumped = json.dumps(schema)
    assert '"title"' not in dumped and '"default"' not in dumped and '"format"' not in dumped


def test_final_schema_only_allows_decide_and_does_not_mutate() -> None:
    assert action_schema(final=True)["properties"]["tool"]["enum"] == ["decide"]
    assert len(action_schema()["properties"]["tool"]["enum"]) == 4
    rf = response_format(final=True)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "agent_action" and rf["json_schema"]["strict"] is True


def test_parse_action_tolerates_fences_and_missing_nulls() -> None:
    a = parse_action(
        '```json\n{"thought": "look it up", "tool": "search_sources", '
        '"query": "EUDR micro", "domain": "ihk.de"}\n```'
    )
    assert a.tool == "search_sources" and a.url is None
    assert a.args() == {"query": "EUDR micro", "domain": "ihk.de"}


@pytest.mark.parametrize(
    "reply,kind",
    [
        ("no json here", "invalid_json"),
        ('{"thought": "x", "tool": "browse"}', "schema"),
        ('{"thought": "x", "tool": "decide", "extra": 1}', "schema"),
    ],
)
def test_parse_action_rejects(reply: str, kind: str) -> None:
    with pytest.raises(PayloadError) as e:
        parse_action(reply)
    assert e.value.kind == kind


def test_as_decision_requires_a_decision() -> None:
    assert parse_action('{"thought": "", "tool": "decide"}').as_decision() is None
    action = AgentAction.model_validate(
        {
            "thought": "",
            "tool": "decide",
            "decision": "applies",
            "reasons": [
                {"text": "t", "fact_ids": [1], "evidence_ids": [], "obligation_numbers": []}
            ],
        }
    )
    d = action.as_decision()
    assert d is not None and d.todos == [] and d.evidence == [] and d.reasons[0].fact_ids == [1]


def test_make_engine_allows_cross_thread_sqlite(tmp_settings: Settings) -> None:
    assert tmp_settings.db_url.startswith("sqlite")
    engine = make_engine(tmp_settings)
    result: list[int] = []

    def use() -> None:
        with engine.connect() as c:
            result.append(c.exec_driver_sql("select 1").scalar_one())

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert result == [1]
