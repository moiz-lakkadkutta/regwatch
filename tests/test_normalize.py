import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from regwatch.config import Settings
from regwatch.schemas import Document
from regwatch.understand import normalize as norm
from regwatch.understand.llm_backend import FixtureLLMBackend, ModelLedger
from regwatch.understand.normalize import (
    NormalizedPayload,
    PayloadError,
    build_messages,
    load_prompt,
    normalize,
    parse_payload,
    response_format,
    response_json_schema,
    to_regchange,
)

EXTRACT = Path(__file__).resolve().parent / "fixtures" / "tavily" / "extract"
URL_2673 = "https://eur-lex.europa.eu/eli/dir/2023/2673/oj"
URL_IHK = "https://www.ihk.de/themen/umwelt-energie/eudr-kleinstunternehmen-fristen"
URL_US = "https://help.shopify.com/en/manual/checkout-settings/refund-return-policy/us-return-refund-rules"


def _doc(doc_id: str, url: str, source_domain: str) -> Document:
    return Document(
        url=url,
        content_hash="h-" + doc_id,
        fetched_at=datetime.now(UTC),
        markdown=(EXTRACT / f"{doc_id}.md").read_text(encoding="utf-8"),
        source_domain=source_domain,
    )


def _fixture_dir(tmp_path: Path, url: str, replies: list[dict[str, Any]]) -> Path:
    root = tmp_path / "normalize"
    root.mkdir(parents=True)
    (root / "one.json").write_text(
        json.dumps({"doc_id": "one", "url": url, "responses": replies}), encoding="utf-8"
    )
    return root


def _reply(payload: dict[str, Any], ptoks: int = 100, ctoks: int = 10) -> dict[str, Any]:
    return {
        "text": json.dumps(payload),
        "usage": {"prompt_tokens": ptoks, "completion_tokens": ctoks},
    }


def _payload(obligations: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
    return {
        "instrument": "Directive (EU) 2023/2673",
        "jurisdiction": "EU",
        "summary": "Offer a two-step withdrawal function by 19 June 2026. It is Article 11a.",
        "effective_dates": ["2026-06-19"],
        "obligations": obligations,
        **extra,
    }


GOOD_QUOTE = (
    "the trader shall ensure that the consumer can also exercise the right of withdrawal "
    "by means of a withdrawal function"
)
BAD_QUOTE = (
    "the trader shall ensure that the consumer can also exercise the right of withdrawal "
    "by means of a withdrawal button"
)


def test_prompt_has_schema_and_verbatim_rule() -> None:
    system, addendum = load_prompt()
    assert "{schema}" not in system
    assert '"additionalProperties": false' in system
    assert '"obligations"' in system and '"quote"' in system
    assert "copied EXACTLY from the document" in system
    assert "German stays German" in system
    assert "Lead with the action, not the law" in system
    assert "{failed_quotes}" in addendum and "NOT present verbatim" in addendum
    assert "Retry addendum" not in system


def test_response_schema_is_strict_compatible() -> None:
    schema = response_json_schema()
    dumped = json.dumps(schema)
    for key in ("title", "default", "format"):
        assert f'"{key}"' not in dumped
    objects = [schema, *schema.get("$defs", {}).values()]
    assert len(objects) == 2  # root + ObligationOut
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert "document_url" not in schema["properties"]
    assert schema["properties"]["effective_dates"] == {"type": "array", "items": {"type": "string"}}
    fmt = response_format()
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == schema


def test_parse_payload_tolerates_fences_and_prose() -> None:
    payload = _payload([{"text": "Provide it.", "quote": GOOD_QUOTE}])
    raw = json.dumps(payload)
    variants = [
        raw,
        f"Here is the JSON:\n```json\n{raw}\n```\nLet me know if you need more.",
        f"<think>\nI should look for obligations {{ carefully }}.\n</think>\n{raw}",
        f"Sure! {raw} Hope this helps.",
    ]
    for text in variants:
        assert parse_payload(text) == NormalizedPayload.model_validate(payload)
    with pytest.raises(PayloadError) as exc:
        parse_payload("no json here")
    assert exc.value.kind == "invalid_json"
    with pytest.raises(PayloadError) as exc:
        parse_payload('{"instrument": 1}')
    assert exc.value.kind == "schema"


def test_to_regchange_keeps_only_iso_dates(caplog: pytest.LogCaptureFixture) -> None:
    payload = NormalizedPayload.model_validate(
        _payload([], effective_dates=["2026-06-19", "June 2026", "2026-13-01", "2025-12-30"])
    )
    with caplog.at_level(logging.WARNING, logger="regwatch.understand.normalize"):
        change = to_regchange(payload, URL_2673)
    assert [d.isoformat() for d in change.effective_dates] == ["2026-06-19", "2025-12-30"]
    assert change.document_url == URL_2673
    assert sum("unparsable effective date" in r.message for r in caplog.records) == 2


def test_quote_validation_drops_non_verbatim(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    replies = [
        _reply(
            _payload(
                [{"text": "Provide.", "quote": GOOD_QUOTE}, {"text": "Bad.", "quote": BAD_QUOTE}]
            )
        ),
        _reply(
            _payload(
                [{"text": "Provide.", "quote": GOOD_QUOTE}, {"text": "Bad.", "quote": BAD_QUOTE}]
            )
        ),
    ]
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, URL_2673, replies))
    settings = Settings()
    ledger = ModelLedger.for_extractor(settings)
    with caplog.at_level(logging.WARNING, logger="regwatch.understand.normalize"):
        result = normalize(
            _doc("eurlex-2023-2673", URL_2673, "eur-lex.europa.eu"),
            backend=backend,
            settings=settings,
            ledger=ledger,
        )
    assert result.error is None and result.change is not None
    assert result.retried is True
    assert result.dropped_quotes == [BAD_QUOTE]
    assert [o.quote for o in result.change.obligations] == [GOOD_QUOTE]
    assert (
        result.change.validate_quotes(
            _doc("eurlex-2023-2673", URL_2673, "eur-lex.europa.eu").markdown
        )
        == []
    )
    assert any(r.message.startswith("dropped quote (") for r in caplog.records)


def test_short_quote_is_offending(tmp_path: Path) -> None:
    short = "the trader shall ensure"  # verbatim but < 6 words
    replies = [_reply(_payload([{"text": "Short.", "quote": short}]))] * 2
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, URL_2673, replies))
    settings = Settings()
    result = normalize(
        _doc("eurlex-2023-2673", URL_2673, "eur-lex.europa.eu"),
        backend=backend,
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    assert result.change is None and result.error == "ungrounded_all_dropped"
    assert result.dropped_quotes == [short]


def test_retry_happens_exactly_once(fixture_llm: FixtureLLMBackend) -> None:
    settings = Settings()
    ledger = ModelLedger.for_extractor(settings)
    ihk = normalize(
        _doc("ihk-eudr-kmu", URL_IHK, "ihk.de"),
        backend=fixture_llm,
        settings=settings,
        ledger=ledger,
    )
    assert ihk.retried is True and ihk.dropped_quotes == [] and ihk.error is None
    assert ihk.change is not None and len(ihk.change.obligations) == 2
    assert len(fixture_llm.calls) == 2
    retry = fixture_llm.calls[1]
    assert [m["role"] for m in retry] == ["system", "user", "assistant", "user"]
    assert "NOT present verbatim" in retry[-1]["content"]
    assert '- "Händler, die keine KMU sind, müssen ab dem 30. Dezember 2025' in retry[-1]["content"]
    assert retry[-1]["content"].endswith(f"Source URL: {URL_IHK}")

    us = normalize(
        _doc("shopify-help-us-returns", URL_US, "help.shopify.com"),
        backend=fixture_llm,
        settings=settings,
        ledger=ledger,
    )
    assert us.change is None and us.error == "ungrounded_all_dropped" and us.retried is True
    assert len(us.dropped_quotes) == 1
    assert len(fixture_llm.calls) == 4  # exactly one retry, never a third call
    assert ledger.n_calls == 4


def test_document_url_is_ours_not_models(tmp_path: Path) -> None:
    stray = _payload(
        [{"text": "Provide.", "quote": GOOD_QUOTE}], document_url="https://evil.example/x"
    )
    clean = _payload([{"text": "Provide.", "quote": GOOD_QUOTE}])
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, URL_2673, [_reply(stray), _reply(clean)]))
    settings = Settings()
    result = normalize(
        _doc("eurlex-2023-2673", URL_2673, "eur-lex.europa.eu"),
        backend=backend,
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    assert result.retried is True and result.error is None
    assert result.change is not None and result.change.document_url == URL_2673
    assert "not a valid JSON object for the schema" in backend.calls[1][-1]["content"]
    assert "document_url" in backend.calls[1][-1]["content"]

    backend2 = FixtureLLMBackend(
        _fixture_dir(tmp_path / "b", URL_2673, [_reply(stray), _reply(stray)])
    )
    result2 = normalize(
        _doc("eurlex-2023-2673", URL_2673, "eur-lex.europa.eu"),
        backend=backend2,
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    assert result2.change is None and result2.error == "schema" and len(backend2.calls) == 2


def test_cost_accounting_sums_both_calls(fixture_llm: FixtureLLMBackend) -> None:
    settings = Settings(
        REGWATCH_EXTRACTOR_PRICE_IN_PER_MTOK=1.0, REGWATCH_EXTRACTOR_PRICE_OUT_PER_MTOK=2.0
    )
    ledger = ModelLedger.for_extractor(settings)
    result = normalize(
        _doc("ihk-eudr-kmu", URL_IHK, "ihk.de"),
        backend=fixture_llm,
        settings=settings,
        ledger=ledger,
    )
    # fixture usage: 1100/300 then 1500/310
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (2600, 610)
    assert result.cost_usd == pytest.approx((2600 * 1.0 + 610 * 2.0) / 1e6)
    assert ledger.cost_usd == pytest.approx(result.cost_usd)
    assert ledger.n_calls == 2 and ledger.prompt_tokens == 2600


def test_markdown_truncated_but_validated_in_full(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    doc = _doc("eurlex-2023-2673", URL_2673, "eur-lex.europa.eu")
    assert doc.markdown.find(GOOD_QUOTE) > 100
    backend = FixtureLLMBackend(
        _fixture_dir(
            tmp_path, URL_2673, [_reply(_payload([{"text": "Provide.", "quote": GOOD_QUOTE}]))]
        )
    )
    settings = Settings(REGWATCH_NORMALIZE_MAX_CHARS=100)
    with caplog.at_level(logging.WARNING, logger="regwatch.understand.normalize"):
        result = normalize(
            doc, backend=backend, settings=settings, ledger=ModelLedger.for_extractor(settings)
        )
    assert result.truncated is True
    assert result.change is not None and result.dropped_quotes == []
    assert GOOD_QUOTE not in backend.calls[0][1]["content"]
    assert any(
        "truncating" in r.message and str(len(doc.markdown)) in r.message for r in caplog.records
    )
    messages, truncated = build_messages(doc, max_chars=10_000)
    assert truncated is False and messages[1]["content"].endswith(f"Source URL: {URL_2673}")
    assert messages[1]["content"].startswith(
        f"Source URL: {URL_2673}\nSource domain: eur-lex.europa.eu\n"
    )


def test_backend_error_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def complete(self, **kw: Any) -> Any:
            raise TimeoutError("slow")

    settings = Settings()
    result = normalize(
        _doc("eurlex-2023-2673", URL_2673, "eur-lex.europa.eu"),
        backend=Boom(),
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    assert result.change is None and result.error == "backend:TimeoutError"
    assert norm.MIN_QUOTE_WORDS == 6
