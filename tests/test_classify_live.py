"""Live smoke test for the classify stage (Nemotron 3 Ultra on Token Factory). Manual only:
`uv run pytest -m live -s tests/test_classify_live.py`; skipped without NEBIUS_API_KEY."""

import json

import pytest

from regwatch.config import Settings
from regwatch.jobs.crawl import SEED_DIR
from regwatch.schemas import Profile, RegChange
from regwatch.understand.classify import classify
from regwatch.understand.llm_backend import LiveLLMBackend, ModelLedger


@pytest.mark.live
def test_classify_seed_withdrawal_change_live() -> None:
    settings = Settings()
    if not settings.nebius_api_key:
        pytest.skip("NEBIUS_API_KEY is empty")
    payload = json.loads((SEED_DIR / "change-001-withdrawal.json").read_text(encoding="utf-8"))
    change = RegChange.model_validate(payload["change"])
    profile = Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    ledger = ModelLedger.for_classifier(settings)
    result = classify(
        change, profile, backend=LiveLLMBackend(settings), settings=settings, ledger=ledger
    )
    print(
        f"\nlive classify: model={result.model} retried={result.retried} "
        f"usage={result.usage} cost_usd={result.cost_usd:.4f} error={result.error} "
        f"dropped={len(result.dropped_todos)} nulled_dues={result.nulled_dues}"
    )
    assert result.error is None and result.applicability is not None
    print(json.dumps(result.applicability.model_dump(mode="json"), indent=2, ensure_ascii=False))
    assert result.applicability.decision == "applies"
    assert result.applicability.ungrounded_todos(change) == []
    assert all(t.citation_url == change.document_url for t in result.applicability.todos)
    assert result.usage is not None and result.usage.prompt_tokens > 0
