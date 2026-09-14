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
