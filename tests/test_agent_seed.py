"""The agent never writes the seed tables: after a full run (it even reads the change's own
document), DocumentRow / RegChangeRow / ApplicabilityRow are byte-for-byte unchanged and
`regwatch seed` still adds 0."""

from typing import Any

from sqlmodel import select

from regwatch.agent.budget import Budget
from regwatch.agent.loop import advance, record_answer
from regwatch.db import (
    AgentPageRow,
    ApplicabilityRow,
    DocumentRow,
    RegChangeRow,
    corpus_counts,
    documents_needing_normalize,
    session,
)
from regwatch.jobs.crawl import seed
from tests.conftest import EUDR_URL, AgentDB, new_run
from tests.fakes import (
    IHK_PAGE,
    IHK_URL,
    ScriptedLLM,
    ScriptedTavily,
    agent_action,
    decide_does_not,
    until_question,
)


def _snapshot(engine: Any) -> dict[str, Any]:
    with session(engine) as s:
        return {
            "docs": sorted(
                (d.url, d.content_hash, d.markdown) for d in s.exec(select(DocumentRow)).all()
            ),
            "changes": sorted(
                (c.id, c.document_url, c.content_hash, c.instrument, c.obligations)
                for c in s.exec(select(RegChangeRow)).all()
            ),
            "appl": sorted(
                (a.id, a.profile_id, a.regchange_id, a.decision, a.reasons, a.todos)
                for a in s.exec(select(ApplicabilityRow)).all()
            ),
            "needing_normalize": [d.url for d in documents_needing_normalize(s)],
            "corpus": corpus_counts(s),
        }


def test_agent_run_leaves_seed_tables_untouched(agent_db: AgentDB) -> None:
    before = _snapshot(agent_db.engine)
    tavily = ScriptedTavily(
        results={"ihk.de": [{"title": "IHK", "url": IHK_URL, "content": "KMU"}]},
        pages={
            IHK_URL: IHK_PAGE,
            EUDR_URL: "# Regulation (EU) 2023/1115\n\nA different basic-depth extract.",
        },
    )
    llm = ScriptedLLM(
        [agent_action("read_source", "Start with the act itself.", url=EUDR_URL), *until_question()]
    )
    run_id = new_run(agent_db)
    kw: dict[str, Any] = {
        "engine": agent_db.engine,
        "settings": agent_db.settings,
        "llm": llm,
        "tavily": tavily,
    }
    assert advance(run_id, budget=Budget(), **kw) == "waiting"
    assert record_answer(agent_db.engine, run_id, 1) == "ok"
    llm.replies.append(decide_does_not(page_id=2))
    assert advance(run_id, budget=Budget(), **kw) == "done"
    with session(agent_db.engine) as s:
        assert len(s.exec(select(AgentPageRow)).all()) == 2
    assert _snapshot(agent_db.engine) == before
    assert seed(agent_db.settings) == 0
    assert _snapshot(agent_db.engine) == before
