import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from regwatch.config import Settings
from regwatch.db import AgentRunRow, AgentStepRow, session
from regwatch.ingest.tavily_backend import FixtureTavilyBackend
from regwatch.schemas import RegChange
from regwatch.understand.llm_backend import FixtureLLMBackend
from tests.fakes import RecordingBackend

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "tavily"
LLM_FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "llm"


@pytest.fixture()
def fixtures_root() -> Path:
    return FIXTURES_ROOT


@pytest.fixture()
def fake_backend(fixtures_root: Path) -> RecordingBackend:
    return RecordingBackend(FixtureTavilyBackend(fixtures_root))


@pytest.fixture()
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'ingest.db'}")


@pytest.fixture()
def manifest(fixtures_root: Path) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(
        (fixtures_root / "manifest.json").read_text(encoding="utf-8")
    )
    return data


@pytest.fixture()
def llm_fixtures_root() -> Path:
    return LLM_FIXTURES_ROOT


@pytest.fixture()
def fixture_llm(llm_fixtures_root: Path) -> FixtureLLMBackend:
    return FixtureLLMBackend(llm_fixtures_root / "normalize")


SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"
# examples/seed-run is recorded by `regwatch record-seed`; tests assert properties, not model text
SEED_CHANGES = len(list(SEED_DIR.glob("change-*.json")))
SEED_FILES = sorted(SEED_DIR.glob("change-*.json"))
# the withdrawal button (new Art. 11a of Directive 2011/83/EU), recorded as one section of 2023/2673
ART_11A = "Directive (EU) 2023/2673 — Article 1, point (3)"


def seed_payload(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def seed_file(instrument: str) -> Path:
    """The seed change file recording `instrument` (exactly one)."""
    (hit,) = [p for p in SEED_FILES if seed_payload(p)["change"]["instrument"] == instrument]
    return hit


def seed_change_id(instrument: str) -> int:
    """RegChangeRow.id of `instrument` after `seed()` into a fresh DB (files load in name order)."""
    return SEED_FILES.index(seed_file(instrument)) + 1


# the seed's EUDR change the classifier left `unclear` (operator vs trader, company size)
EUDR_UNCLEAR = "Regulation (EU) 2023/1115 — Article 6 – Article 8"
EUDR_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML?uri=CELEX%3A02023R1115-20251226"


def eudr_change() -> RegChange:
    """The seed's unclear EUDR RegChange, shared by the agent grounding and loop tests."""
    return RegChange.model_validate(seed_payload(seed_file(EUDR_UNCLEAR))["change"])


def make_agent_run(
    engine: Any,
    *,
    visitor: str = "v",
    change: int = 14,
    status: str = "running",
    n_steps: int = 0,
    credits: int = 0,
    created_at: datetime | None = None,
    profile_id: int = 1,
) -> int:
    """Insert an AgentRunRow and return its id (shared by the agent budget/runner/route tests)."""
    now = created_at or datetime(2026, 10, 20, 12, 0)
    with session(engine) as s:
        row = AgentRunRow(
            visitor_id=visitor,
            profile_id=profile_id,
            regchange_id=change,
            status=status,
            n_steps=n_steps,
            tavily_credits=credits,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.id is not None
        return row.id


def make_agent_step(
    engine: Any, run_id: int, *, usd: float = 0.0, credits: int = 0, at: datetime | None = None
) -> None:
    """Insert one settled AgentStepRow carrying `usd` and `credits` of spend."""
    with session(engine) as s:
        s.add(
            AgentStepRow(
                run_id=run_id,
                idx=1,
                tool="search_sources",
                cost_usd=usd,
                credits=credits,
                created_at=at or datetime(2026, 10, 20, 12, 0),
            )
        )
        s.commit()
