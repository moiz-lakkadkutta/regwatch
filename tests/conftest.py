import json
from pathlib import Path
from typing import Any

import pytest

from regwatch.config import Settings
from regwatch.ingest.tavily_backend import FixtureTavilyBackend
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
