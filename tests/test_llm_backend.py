import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from regwatch.config import Settings
from regwatch.understand import llm_backend
from regwatch.understand.llm_backend import (
    FixtureLLMBackend,
    FixtureMissing,
    LiveLLMBackend,
    LLMUsage,
    ModelLedger,
    make_llm_backend,
    source_url_of,
)


def _messages(url: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": f"Source URL: {url}\n\n<document>x</document>\n\nSource URL: {url}",
        },
    ]


def test_ledger_cost_uses_settings_prices() -> None:
    settings = Settings(
        REGWATCH_EXTRACTOR_PRICE_IN_PER_MTOK=1.0, REGWATCH_EXTRACTOR_PRICE_OUT_PER_MTOK=10.0
    )
    ledger = ModelLedger.for_extractor(settings)
    first = ledger.add(LLMUsage(prompt_tokens=1_000_000, completion_tokens=100_000))
    assert first == pytest.approx(2.0)
    second = ledger.add(LLMUsage(prompt_tokens=500_000, completion_tokens=0))
    assert second == pytest.approx(0.5)
    assert ledger.n_calls == 2
    assert ledger.prompt_tokens == 1_500_000 and ledger.completion_tokens == 100_000
    assert ledger.cost_usd == pytest.approx(2.5)
    default = ModelLedger.for_extractor(Settings())
    assert default.add(LLMUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)) == (
        pytest.approx(
            Settings().extractor_price_in_per_mtok + Settings().extractor_price_out_per_mtok
        )
    )


def test_fixture_backend_replays_in_order_then_raises(tmp_path: Path) -> None:
    url = "https://eur-lex.europa.eu/eli/dir/2023/2673/oj"
    (tmp_path / "a.json").write_text(
        json.dumps(
            {
                "doc_id": "a",
                "url": url,
                "responses": [
                    {"text": "first", "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
                    {"text": "second", "usage": {"prompt_tokens": 20, "completion_tokens": 2}},
                ],
            }
        ),
        encoding="utf-8",
    )
    backend = FixtureLLMBackend(tmp_path)
    r1 = backend.complete(model="m", messages=_messages(url), response_format=None)
    r2 = backend.complete(model="m", messages=_messages(url), response_format=None)
    assert (r1.text, r1.usage.prompt_tokens, r1.model) == ("first", 10, "m")
    assert (r2.text, r2.usage.completion_tokens) == ("second", 2)
    assert len(backend.calls) == 2 and backend.calls[0][1]["content"].endswith(url)
    with pytest.raises(FixtureMissing):
        backend.complete(model="m", messages=_messages(url), response_format=None)
    with pytest.raises(FixtureMissing):
        backend.complete(
            model="m", messages=_messages("https://example.invalid/x"), response_format=None
        )
    with pytest.raises(FixtureMissing):
        backend.complete(
            model="m", messages=[{"role": "user", "content": "no key"}], response_format=None
        )
    assert source_url_of(_messages(url)) == url


def test_live_backend_refuses_empty_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    assert llm_backend.__name__ == "regwatch.understand.llm_backend"
    assert "openai" not in sys.modules
    settings = Settings(NEBIUS_API_KEY="", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        LiveLLMBackend(settings)
    with pytest.raises(ValueError):
        make_llm_backend("live", settings)
    assert "openai" not in sys.modules
    backend = make_llm_backend("fixtures", settings)
    assert isinstance(backend, FixtureLLMBackend)
    assert backend.root.name == "normalize"


@pytest.mark.live
def test_live_backend_skipped_without_key() -> None:
    settings = Settings()
    if not settings.nebius_api_key:
        pytest.skip("NEBIUS_API_KEY is empty")
    backend = LiveLLMBackend(settings)
    result = backend.complete(
        model=settings.extractor_model,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        response_format=None,
    )
    assert result.text.strip()
    assert result.usage.prompt_tokens > 0


class _RecordingCompletions:
    """Stands in for `client.chat.completions`; returns one canned reply per call."""

    def __init__(self, finish_reason: str = "stop") -> None:
        self.calls: list[dict[str, object]] = []
        self.finish_reason = finish_reason

    def create(self, **kw: object) -> SimpleNamespace:
        self.calls.append(kw)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish_reason, message=SimpleNamespace(content="{}")
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4096),
            model=kw["model"],
        )


def _live_with(completions: _RecordingCompletions) -> tuple[LiveLLMBackend, Settings]:
    settings = Settings(NEBIUS_API_KEY="k", _env_file=None)  # type: ignore[call-arg]
    backend = LiveLLMBackend(settings)
    backend._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return backend, settings


def test_live_backend_turns_thinking_off_for_the_extractor_only() -> None:
    completions = _RecordingCompletions()
    backend, settings = _live_with(completions)
    backend.complete(model=settings.extractor_model, messages=_messages("u"), response_format=None)
    backend.complete(model=settings.classifier_model, messages=_messages("u"), response_format=None)
    extractor_call, classifier_call = completions.calls
    assert extractor_call["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in classifier_call


def test_live_backend_extractor_thinking_can_be_turned_back_on() -> None:
    completions = _RecordingCompletions()
    settings = Settings(  # type: ignore[call-arg]
        NEBIUS_API_KEY="k", REGWATCH_EXTRACTOR_THINKING=True, _env_file=None
    )
    backend = LiveLLMBackend(settings)
    backend._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    backend.complete(model=settings.extractor_model, messages=_messages("u"), response_format=None)
    assert "extra_body" not in completions.calls[0]


def test_live_backend_warns_when_the_reply_hits_max_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, settings = _live_with(_RecordingCompletions(finish_reason="length"))
    with caplog.at_level("WARNING", logger="regwatch.understand.llm_backend"):
        backend.complete(
            model=settings.extractor_model, messages=_messages("u"), response_format=None
        )
    assert any("finish_reason=length" in r.getMessage() for r in caplog.records)
