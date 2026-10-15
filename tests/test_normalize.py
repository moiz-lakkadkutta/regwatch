import json
import logging
import re
import shutil
import threading
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, col, select

from regwatch.config import Settings
from regwatch.db import (
    CrawlRunRow,
    DocumentRow,
    RegChangeRow,
    documents_needing_normalize,
    make_engine,
)
from regwatch.ingest.diff import content_hash
from regwatch.jobs.crawl import crawl, main, seed, summary_lines
from regwatch.schemas import Document
from regwatch.understand import normalize as norm
from regwatch.understand.llm_backend import FixtureLLMBackend, LLMResult, LLMUsage, ModelLedger
from regwatch.understand.normalize import (
    NormalizedPayload,
    PayloadError,
    build_messages,
    load_prompt,
    normalize,
    normalize_act,
    normalize_all,
    parse_payload,
    response_format,
    response_json_schema,
    to_regchange,
)
from regwatch.understand.sections import is_long_act, split_act
from tests.conftest import LLM_FIXTURES_ROOT, SEED_CHANGES

EXTRACT = Path(__file__).resolve().parent / "fixtures" / "tavily" / "extract"
URL_2673 = "https://eur-lex.europa.eu/eli/dir/2023/2673/oj"
URL_IHK = "https://www.ihk.de/themen/umwelt-energie/eudr-kleinstunternehmen-fristen"
URL_2011_83 = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32011L0083"
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


# --- normalize_all over the DB, crawl stage ---------------------------------------------------


def _manifest_rows(manifest: list[dict[str, Any]]) -> list[DocumentRow]:
    rows = []
    for m in manifest:
        markdown = (EXTRACT / f"{m['id']}.md").read_text(encoding="utf-8")
        rows.append(
            DocumentRow(
                url=m["url"],
                content_hash=content_hash(markdown),
                source_domain=m["source_domain"],
                markdown=markdown,
                regulation_tags=json.dumps(m["regulation_tags"]),
            )
        )
    return rows


def test_normalize_all_rate_at_least_80pct(
    tmp_settings: Settings, manifest: list[dict[str, Any]], fixture_llm: FixtureLLMBackend
) -> None:
    engine = make_engine(tmp_settings)
    with Session(engine) as s:
        s.add_all(_manifest_rows(manifest))
        s.commit()
        ledger = ModelLedger.for_extractor(tmp_settings)
        summary = normalize_all(s, backend=fixture_llm, settings=tmp_settings, ledger=ledger)
    assert summary.n_docs == 16
    assert (summary.n_ok_first_try, summary.n_retried, summary.n_failed) == (14, 1, 1)
    assert summary.n_ok == 15 and summary.n_changes_new == 15
    assert summary.success_rate_no_retry >= 0.8
    assert summary.line().startswith(
        "normalize: 16 documents · 14 ok first try (88%) · 1 retried · 1 failed · $"
    )
    assert summary.cost_usd == pytest.approx(ledger.cost_usd) and summary.cost_usd > 0
    assert summary.prompt_tokens == ledger.prompt_tokens > 0
    assert len(fixture_llm.calls) == 18
    with Session(engine) as s:
        changes = s.exec(select(RegChangeRow)).all()
        assert len(changes) == 15
        assert all(c.model == tmp_settings.extractor_model and c.content_hash for c in changes)
        assert not any(c.document_url == URL_US for c in changes)
        # every stored change is grounded in its document, checked by the code, not the prompt
        for c in changes:
            doc = s.get(DocumentRow, c.document_url)
            assert doc is not None and c.content_hash == doc.content_hash
            assert c.to_schema().validate_quotes(doc.markdown) == []
        ihk = next(c for c in changes if c.document_url == URL_IHK)
        assert len(ihk.to_schema().obligations) == 2
        assert documents_needing_normalize(s) == [s.get(DocumentRow, URL_US)]


def test_normalize_all_idempotent_by_content_hash(
    tmp_settings: Settings, manifest: list[dict[str, Any]], llm_fixtures_root: Path
) -> None:
    engine = make_engine(tmp_settings)
    with Session(engine) as s:
        s.add_all(_manifest_rows(manifest))
        s.commit()
        first = FixtureLLMBackend(llm_fixtures_root / "normalize")
        normalize_all(s, backend=first, settings=tmp_settings)
        assert len(first.calls) == 18
        # second run: only the failed doc is retried (nothing stored for it), no other calls
        second = FixtureLLMBackend(llm_fixtures_root / "normalize")
        again = normalize_all(s, backend=second, settings=tmp_settings)
        assert again.n_docs == 1 and again.n_failed == 1 and again.n_changes_new == 0
        assert [m.get("content", "").endswith(URL_US) for m in second.calls[0]][-1]
        assert len(second.calls) == 2
        # bumping one document's content hash re-normalises exactly that one
        row = s.get(DocumentRow, URL_2673)
        assert row is not None
        row.content_hash = "changed-" + row.content_hash
        s.add(row)
        s.commit()
        third = FixtureLLMBackend(llm_fixtures_root / "normalize")
        third_summary = normalize_all(s, backend=third, settings=tmp_settings)
        assert third_summary.n_docs == 2 and third_summary.n_changes_new == 1
        urls = {
            m["content"].splitlines()[-1]
            for call in third.calls
            for m in call
            if m["role"] == "user"
        }
        assert urls == {f"Source URL: {URL_2673}", f"Source URL: {URL_US}"}
        rows_2673 = s.exec(select(RegChangeRow).where(RegChangeRow.document_url == URL_2673)).all()
        assert len(rows_2673) == 2 and {r.content_hash for r in rows_2673} == {
            row.content_hash,
            row.content_hash.removeprefix("changed-"),
        }


def test_crawl_stage_normalize_updates_run_row_and_prints_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_url = f"sqlite:///{tmp_path / 'crawl.db'}"
    monkeypatch.setenv("REGWATCH_DB_URL", db_url)
    assert main(["--stage", "ingest", "--backend", "fixtures", "--no-out"]) == 0
    capsys.readouterr()
    with caplog.at_level(logging.INFO, logger="regwatch.understand.normalize"):
        assert main(["--stage", "normalize", "--llm-backend", "fixtures"]) == 0
    err = capsys.readouterr().err
    assert "normalize: 16 documents · 14 ok first try (88%) · 1 retried · 1 failed · $" in err
    usage_lines = [
        r.message
        for r in caplog.records
        if re.match(r"model=\S+ prompt_tokens=\d+ completion_tokens=\d+ cost_usd=", r.message)
    ]
    assert len(usage_lines) == 18  # one INFO per model call, retries included
    with Session(make_engine(Settings(REGWATCH_DB_URL=db_url))) as s:
        runs = s.exec(select(CrawlRunRow).order_by(col(CrawlRunRow.id))).all()
        assert [r.stage for r in runs] == ["ingest", "normalize"]
        run = runs[-1]
        assert run.model_cost_usd > 0
        assert run.n_changes_new == 15
        assert (run.n_normalize_retried, run.n_normalize_failed) == (1, 1)
        assert run.finished_at is not None and run.tavily_credits == 0
        assert runs[0].model_cost_usd == 0.0

    # --stage all on a fresh DB: one row carries ingest, normalize and classify (no profile → 0)
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'all.db'}")
    rc = main(["--stage", "all", "--backend", "fixtures", "--llm-backend", "fixtures"])
    assert rc == 0
    err = capsys.readouterr().err
    lines = [
        line
        for line in err.splitlines()
        if line.startswith(("ingest:", "normalize:", "classify:", "crawl:"))
    ]
    assert lines[0].startswith("ingest: 16 documents (16 new")
    assert lines[1].startswith("normalize: 16 documents · 14 ok first try (88%)")
    assert lines[2].startswith("classify: 0 changes")
    assert lines[3].startswith("crawl: 16 new documents · 15 new changes · 0 classified")
    with Session(make_engine(Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'all.db'}"))) as s:
        runs = s.exec(select(CrawlRunRow)).all()
        assert len(runs) == 1 and runs[0].stage == "all"
        assert runs[0].tavily_credits == 56 and runs[0].model_cost_usd > 0
        assert runs[0].n_changes_new == 15
    assert not (tmp_path / "documents.jsonl").exists()


def test_seed_then_normalize_adds_zero_for_seed_docs(
    tmp_settings: Settings, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert seed(tmp_settings) == SEED_CHANGES
    assert seed(tmp_settings) == 0
    with Session(make_engine(tmp_settings)) as s:
        changes = s.exec(select(RegChangeRow)).all()
        assert len(changes) == SEED_CHANGES
        for c in changes:
            doc = s.get(DocumentRow, c.document_url)
            assert doc is not None and c.content_hash == doc.content_hash != ""
            assert c.model == "seed"
        assert documents_needing_normalize(s) == []
    backend = FixtureLLMBackend(LLM_FIXTURES_ROOT / "normalize")
    run = crawl(tmp_settings, stage="normalize", llm_backend=backend)
    assert backend.calls == []
    assert run.stage == "normalize" and run.n_changes_new == 0 and run.model_cost_usd == 0.0
    monkeypatch.setenv("REGWATCH_DB_URL", tmp_settings.db_url)
    assert main(["--stage", "normalize", "--llm-backend", "fixtures"]) == 0
    assert (
        "normalize: 0 documents · 0 ok first try (0%) · 0 retried · 0 failed"
        in capsys.readouterr().err
    )
    with Session(make_engine(tmp_settings)) as s:
        assert len(s.exec(select(RegChangeRow)).all()) == SEED_CHANGES


# --- concurrency -------------------------------------------------------------------------------


def _with_concurrency(settings: Settings, n: int) -> Settings:
    return settings.model_copy(update={"normalize_concurrency": n})


def _run_all(
    settings: Settings, rows: list[DocumentRow], backend: Any
) -> tuple[Any, ModelLedger, list[tuple[str, str, str]]]:
    engine = make_engine(settings)
    with Session(engine) as s:
        s.add_all(rows)
        s.commit()
        ledger = ModelLedger.for_extractor(settings)
        summary = normalize_all(s, backend=backend, settings=settings, ledger=ledger)
    with Session(engine) as s:
        stored = sorted(
            (c.document_url, c.content_hash, c.obligations)
            for c in s.exec(select(RegChangeRow)).all()
        )
    return summary, ledger, stored


def test_normalize_concurrency_default_is_4(tmp_settings: Settings) -> None:
    assert tmp_settings.normalize_concurrency == 4


def test_normalize_all_same_result_at_concurrency_1_and_4(
    tmp_path: Path, manifest: list[dict[str, Any]], llm_fixtures_root: Path
) -> None:
    outcomes = []
    for n in (1, 4):
        settings = Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / f'c{n}.db'}")
        backend = FixtureLLMBackend(llm_fixtures_root / "normalize")
        outcomes.append(_run_all(_with_concurrency(settings, n), _manifest_rows(manifest), backend))
    (sum1, led1, stored1), (sum4, led4, stored4) = outcomes
    assert stored1 == stored4 and len(stored1) == 15
    assert asdict(sum1) == pytest.approx(asdict(sum4))
    assert (led1.n_calls, led1.prompt_tokens, led1.completion_tokens) == (
        led4.n_calls,
        led4.prompt_tokens,
        led4.completion_tokens,
    )
    assert led1.cost_usd == pytest.approx(led4.cost_usd)


class _StubBackend:
    """Replies with a grounded payload for every document; optional sleep / one failing URL."""

    def __init__(self, *, delay: float = 0.0, fail_url: str | None = None) -> None:
        self.delay = delay
        self.fail_url = fail_url
        self.threads: set[int] = set()

    def complete(
        self, *, model: str, messages: list[dict[str, str]], response_format: Any
    ) -> LLMResult:
        self.threads.add(threading.get_ident())
        if self.fail_url and messages[-1]["content"].endswith(self.fail_url):
            raise RuntimeError("boom")
        time.sleep(self.delay)
        text = json.dumps(_payload([{"text": "Provide it.", "quote": GOOD_QUOTE}]))
        return LLMResult(
            text=text, usage=LLMUsage(prompt_tokens=100, completion_tokens=10), model=model
        )


def _stub_rows(n: int) -> list[DocumentRow]:
    markdown = (EXTRACT / "eurlex-2023-2673.md").read_text(encoding="utf-8")
    return [
        DocumentRow(
            url=f"https://eur-lex.europa.eu/stub/{i}",
            content_hash=f"h{i}",
            source_domain="eur-lex.europa.eu",
            markdown=markdown,
            regulation_tags=json.dumps(["withdrawal"]),
        )
        for i in range(n)
    ]


def test_normalize_all_overlaps_model_calls(tmp_settings: Settings) -> None:
    backend = _StubBackend(delay=0.2)
    start = time.monotonic()
    summary, ledger, stored = _run_all(_with_concurrency(tmp_settings, 4), _stub_rows(4), backend)
    assert time.monotonic() - start < 0.6
    assert summary.n_ok == 4 and len(stored) == 4 and ledger.n_calls == 4
    assert ledger.prompt_tokens == 400 and len(backend.threads) > 1


def test_normalize_all_one_exception_fails_only_that_document(
    tmp_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    bad = "https://eur-lex.europa.eu/stub/2"
    backend = _StubBackend(fail_url=bad)
    with caplog.at_level(logging.WARNING):
        summary, _, stored = _run_all(_with_concurrency(tmp_settings, 4), _stub_rows(4), backend)
    assert summary.n_failed == 1 and summary.n_ok == 3
    assert [u for u, _, _ in stored] == [f"https://eur-lex.europa.eu/stub/{i}" for i in (0, 1, 3)]
    assert any(bad in r.getMessage() for r in caplog.records)


def test_normalize_all_survives_normalize_raising(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = "https://eur-lex.europa.eu/stub/1"
    real = norm.normalize

    def flaky(document: Document, **kwargs: Any) -> Any:
        if document.url == bad:
            raise ValueError("unexpected")
        return real(document, **kwargs)

    monkeypatch.setattr(norm, "normalize", flaky)
    with caplog.at_level(logging.ERROR):
        summary, ledger, stored = _run_all(
            _with_concurrency(tmp_settings, 4), _stub_rows(3), _StubBackend()
        )
    assert summary.n_failed == 1 and len(stored) == 2 and ledger.n_calls == 2
    assert any(
        bad in r.getMessage() and r.levelno == logging.ERROR and r.exc_info for r in caplog.records
    )


def test_normalize_all_stops_paying_when_the_calling_thread_raises(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _CountingBackend(_StubBackend):
        def __init__(self) -> None:
            super().__init__(delay=0.05)
            self.n_calls = 0
            self._lock = threading.Lock()

        def complete(
            self, *, model: str, messages: list[dict[str, str]], response_format: Any
        ) -> LLMResult:
            with self._lock:
                self.n_calls += 1
            return super().complete(model=model, messages=messages, response_format=response_format)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store failed")

    monkeypatch.setattr(norm, "store_regchange", boom)
    backend = _CountingBackend()
    with pytest.raises(RuntimeError, match="store failed"):
        _run_all(_with_concurrency(tmp_settings, 2), _stub_rows(12), backend)
    assert backend.n_calls < 12


def test_default_input_cap_reaches_late_articles() -> None:
    # Directive (EU) 2023/2673 inserts Art. 11a (withdrawal function) at char ~66,000 of ~106,000.
    assert Settings(_env_file=None).normalize_max_chars >= 120_000  # type: ignore[call-arg]


# --- long acts: one whole-act call for metadata, then one call per section -------------------

ACT_URL = "https://eur-lex.europa.eu/eli/dir/2099/1/oj"
ACT_QUOTE_1 = "the trader shall keep a record of every distance contract concluded with a consumer"
ACT_QUOTE_2 = (
    "the trader shall ensure that the consumer can also exercise the right of withdrawal "
    "by means of a withdrawal function"
)
ACT_MARKDOWN = (
    "Directive (EU) 2099/1 of the European Parliament and of the Council amending Directive "
    "2011/83/EU as regards distance contracts\n\n"
    + "".join(
        f"({i}) Recital {i}: consumers buying at a distance should be able to rely on clear "
        "information and a simple way to withdraw from contracts concluded online.\n\n"
        for i in range(1, 15)
    )
    + "HAVE ADOPTED THIS DIRECTIVE:\n\n"
    "Article 1\n\n"
    "Directive 2011/83/EU is amended as follows:\n\n"
    f"(1)Article 3 is amended as follows: {ACT_QUOTE_1} for five years.\n\n"
    "(2)the following Article is inserted:\n\n"
    f"'Article 11a Withdrawal function: For distance contracts concluded by means of an online "
    f"interface, {ACT_QUOTE_2}.'\n\n"
    "Article 2\n\n"
    "Member States shall adopt and publish the laws necessary to comply with this Directive.\n\n"
    "Article 3\n\n"
    "This Directive is addressed to the Member States.\n"
)
ACT_MIN_CHARS = 2_800  # above every short EUR-Lex fixture (≤ 2,687), below this act


def _act_settings(tmp_path: Path) -> Settings:
    return Settings(
        REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'act.db'}",
        REGWATCH_ACT_SPLIT_MIN_CHARS=ACT_MIN_CHARS,
    )


def _act_row() -> DocumentRow:
    return DocumentRow(
        url=ACT_URL,
        content_hash=content_hash(ACT_MARKDOWN),
        source_domain="eur-lex.europa.eu",
        markdown=ACT_MARKDOWN,
        regulation_tags=json.dumps(["withdrawal"]),
    )


def _section_payload(
    obligations: list[dict[str, str]], *, jurisdiction: str = "EU", dates: list[str] | None = None
) -> dict[str, Any]:
    return _payload(
        obligations,
        instrument="ignored for sections",
        jurisdiction=jurisdiction,
        effective_dates=dates or [],
    )


def _whole_act_reply() -> dict[str, Any]:
    return _reply(
        _payload(
            [{"text": "Offer a withdrawal function.", "quote": ACT_QUOTE_2}],
            instrument="Directive (EU) 2099/1",
            effective_dates=["2026-06-19"],
        )
    )


def test_synthetic_act_is_long_and_splits_into_three_sections(tmp_path: Path) -> None:
    settings = _act_settings(tmp_path)
    doc = _act_row().to_schema()
    assert is_long_act(doc, settings)
    assert [s.label for s in split_act(ACT_MARKDOWN)] == [
        "Article 1, point (1)",
        "Article 1, point (2)",
        "Article 2 – Article 3",
    ]


def test_normalize_all_stores_one_change_per_section(
    tmp_path: Path, manifest: list[dict[str, Any]]
) -> None:
    settings = _act_settings(tmp_path)
    replies = [
        _whole_act_reply(),
        # point (1): jurisdiction unclear, no dates -> act's jurisdiction and dates
        _reply(
            _section_payload(
                [{"text": "Keep contract records.", "quote": ACT_QUOTE_1}], jurisdiction="unclear"
            )
        ),
        # point (2): own date wins
        _reply(
            _section_payload(
                [{"text": "Offer a withdrawal function.", "quote": ACT_QUOTE_2}],
                dates=["2026-07-01"],
            )
        ),
        # Articles 2-3: nothing for a merchant -> not stored
        _reply(_section_payload([])),
    ]
    root = _fixture_dir(tmp_path, ACT_URL, replies)
    shutil.copy(LLM_FIXTURES_ROOT / "normalize" / "eurlex-2011-83.json", root)
    shutil.copy(LLM_FIXTURES_ROOT / "normalize" / "ihk-eudr-kmu.json", root)
    backend = FixtureLLMBackend(root)
    short = [m for m in _manifest_rows(manifest) if m.url in (URL_2011_83, URL_IHK)]
    engine = make_engine(settings)
    with Session(engine) as s:
        s.add_all([_act_row(), *short])
        s.commit()
        ledger = ModelLedger.for_extractor(settings)
        summary = normalize_all(s, backend=backend, settings=settings, ledger=ledger)
    assert summary.n_docs == 3 and summary.n_failed == 0 and summary.n_ok == 3
    assert summary.n_changes_new == 4  # 2 sections + 2 short documents
    # the recorded IHK reply is the one that needs its retry; the act and 2011/83 do not
    assert summary.line().startswith(
        "normalize: 3 documents · 2 ok first try (67%) · 1 retried · 0 failed · $"
    )
    act_calls = [c for c in backend.calls if c[-1]["content"].endswith(ACT_URL)]
    assert len(act_calls) == 4 and ledger.n_calls == len(backend.calls)
    assert summary.prompt_tokens == ledger.prompt_tokens
    assert summary.cost_usd == pytest.approx(ledger.cost_usd)
    # section messages: Source URL first and last, the section hint, only the section's text
    user = act_calls[2][-1]["content"]
    assert user.splitlines()[0] == f"Source URL: {ACT_URL}"
    assert user.splitlines()[-1] == f"Source URL: {ACT_URL}"
    assert (
        "Section: Article 1, point (2) of Directive (EU) 2099/1. Extract obligations from this "
        "section only; other sections of the act are processed separately. The amended act named "
        "in the section text decides who an obligation binds." in user
    )
    assert ACT_QUOTE_2 in user and ACT_QUOTE_1 not in user and "Recital 3" not in user
    assert user.index("Section: ") < user.index("<document>")
    with Session(engine) as s:
        rows = s.exec(select(RegChangeRow).order_by(col(RegChangeRow.id))).all()
        act_rows = [r for r in rows if r.document_url == ACT_URL]
        assert [r.instrument for r in act_rows] == [
            "Directive (EU) 2099/1 — Article 1, point (1)",
            "Directive (EU) 2099/1 — Article 1, point (2)",
        ]
        assert [r.jurisdiction for r in act_rows] == ["EU", "EU"]
        assert [r.to_schema().effective_dates for r in act_rows] == [
            [date(2026, 6, 19)],
            [date(2026, 7, 1)],
        ]
        assert all(r.content_hash == content_hash(ACT_MARKDOWN) for r in act_rows)
        for r in act_rows:
            assert r.to_schema().validate_quotes(ACT_MARKDOWN) == []
        assert len([r for r in rows if r.document_url == URL_2011_83]) == 1
        assert len([r for r in rows if r.document_url == URL_IHK]) == 1
        assert documents_needing_normalize(s) == []


def test_section_with_ungrounded_quote_is_retried_then_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _act_settings(tmp_path)
    bad = [{"text": "Offer a button.", "quote": BAD_QUOTE}]
    replies = [
        _whole_act_reply(),
        _reply(_section_payload([{"text": "Keep contract records.", "quote": ACT_QUOTE_1}])),
        _reply(_section_payload(bad)),
        _reply(_section_payload(bad)),  # the one retry, still ungrounded
        _reply(_section_payload([])),
    ]
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, ACT_URL, replies))
    engine = make_engine(settings)
    with Session(engine) as s, caplog.at_level(logging.WARNING):
        s.add(_act_row())
        s.commit()
        summary = normalize_all(s, backend=backend, settings=settings)
    assert len(backend.calls) == 5
    assert "withdrawal button" in backend.calls[3][-1]["content"]  # retry lists the bad quote
    assert summary.n_docs == 1 and summary.n_ok == 1 and summary.n_changes_new == 1
    assert summary.n_retried == 1 and summary.n_failed == 0
    assert any("Article 1, point (2)" in r.getMessage() for r in caplog.records)
    assert any(
        r.getMessage() == f"act {ACT_URL}: 1 of 3 sections stored, 1 failed" for r in caplog.records
    )
    with Session(engine) as s:
        rows = s.exec(select(RegChangeRow)).all()
        assert [r.instrument for r in rows] == ["Directive (EU) 2099/1 — Article 1, point (1)"]


def test_act_metadata_falls_back_when_whole_act_call_fails(tmp_path: Path) -> None:
    settings = _act_settings(tmp_path)
    replies = [
        {"text": "not json", "usage": {}},
        {"text": "still not json", "usage": {}},
        _reply(_section_payload([{"text": "Keep records.", "quote": ACT_QUOTE_1}])),
        _reply(_section_payload([])),
        _reply(_section_payload([])),
    ]
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, ACT_URL, replies))
    result = normalize_act(
        _act_row().to_schema(),
        backend=backend,
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    first_line = ACT_MARKDOWN.splitlines()[0][:120]
    assert len(backend.calls) == 5
    assert f"of {first_line}." in backend.calls[2][-1]["content"]
    assert [c.instrument for c in result.changes] == [
        "Directive (EU) 2099/1 — Article 1, point (1)"
    ]
    assert result.changes[0].jurisdiction == "EU"
    assert result.changes[0].effective_dates == []


def test_act_with_no_grounded_section_fails_and_stores_nothing(tmp_path: Path) -> None:
    settings = _act_settings(tmp_path)
    replies = [_whole_act_reply(), *[_reply(_section_payload([])) for _ in range(3)]]
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, ACT_URL, replies))
    engine = make_engine(settings)
    with Session(engine) as s:
        s.add(_act_row())
        s.commit()
        summary = normalize_all(s, backend=backend, settings=settings)
        assert summary.n_failed == 1 and summary.n_ok == 0 and summary.n_changes_new == 0
        assert s.exec(select(RegChangeRow)).all() == []


def test_whole_act_call_is_metadata_only_no_quote_retry(tmp_path: Path) -> None:
    """The whole-act obligations are discarded, so a bad quote there costs no retry and the
    model's instrument is still used (not the first-line fallback)."""
    settings = _act_settings(tmp_path)
    whole = _payload(
        [{"text": "Offer a button.", "quote": BAD_QUOTE}],
        instrument="Directive (EU) 2099/1",
        jurisdiction="EU",
        effective_dates=["2026-06-19"],
    )
    replies = [
        _reply(whole),
        _reply(_section_payload([{"text": "Keep records.", "quote": ACT_QUOTE_1}])),
        _reply(_section_payload([])),
        _reply(_section_payload([])),
    ]
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, ACT_URL, replies))
    result = normalize_act(
        _act_row().to_schema(),
        backend=backend,
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    assert len(backend.calls) == 1 + len(split_act(ACT_MARKDOWN))
    assert not result.retried
    assert [c.instrument for c in result.changes] == [
        "Directive (EU) 2099/1 — Article 1, point (1)"
    ]
    assert result.changes[0].effective_dates == [date(2026, 6, 19)]


def test_crawl_lines_count_a_long_act_as_one_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_url = f"sqlite:///{tmp_path / 'lines.db'}"
    monkeypatch.setenv("REGWATCH_DB_URL", db_url)
    monkeypatch.setenv("REGWATCH_ACT_SPLIT_MIN_CHARS", str(ACT_MIN_CHARS))
    llm_dir = tmp_path / "llm"
    shutil.copytree(LLM_FIXTURES_ROOT / "classify", llm_dir / "classify")
    replies = [
        _whole_act_reply(),
        _reply(_section_payload([{"text": "Keep records.", "quote": ACT_QUOTE_1}])),
        _reply(_section_payload([{"text": "Offer it.", "quote": ACT_QUOTE_2}])),
        _reply(_section_payload([])),
    ]
    _fixture_dir(llm_dir, ACT_URL, replies)  # llm/normalize/one.json
    with Session(make_engine(Settings(REGWATCH_DB_URL=db_url))) as s:
        s.add(_act_row())
        s.commit()
    argv = ["--stage", "normalize", "--llm-backend", "fixtures", "--llm-fixtures-dir", str(llm_dir)]
    assert main(argv) == 0
    err = capsys.readouterr().err
    assert "normalize: 1 documents · 1 ok first try (100%) · 0 retried · 0 failed · $" in err
    with Session(make_engine(Settings(REGWATCH_DB_URL=db_url))) as s:
        run = s.exec(select(CrawlRunRow).order_by(col(CrawlRunRow.id))).all()[-1]
    assert run.n_changes_new == 2
    # rebuilt from the row alone (no in-memory summary) the line never claims documents
    (line,) = summary_lines(run, None)
    assert line.startswith("normalize: 2 new changes · 0 retried · 0 failed · $")
    # `--stage all`: the normalize line counts the act once, the crawl line its 2 changes
    all_url = f"sqlite:///{tmp_path / 'all.db'}"
    monkeypatch.setenv("REGWATCH_DB_URL", all_url)
    for path in (LLM_FIXTURES_ROOT / "normalize").glob("*.json"):
        shutil.copy(path, llm_dir / "normalize")
    (llm_dir / "normalize" / "one.json").write_text(
        json.dumps({"url": ACT_URL, "responses": replies}), encoding="utf-8"
    )
    with Session(make_engine(Settings(REGWATCH_DB_URL=all_url))) as s:
        s.add(_act_row())
        s.commit()
    argv = ["--stage", "all", "--backend", "fixtures", "--llm-backend", "fixtures"]
    assert main([*argv, "--llm-fixtures-dir", str(llm_dir), "--no-out"]) == 0
    lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(("normalize:", "crawl:"))
    ]
    assert lines[0].startswith("normalize: 17 documents · 15 ok first try (88%) · 1 retried")
    assert lines[1].startswith("crawl: 16 new documents · 17 new changes · ")


class _FailOnCall:
    """Delegates to a fixture backend but raises on the n-th call (1-based)."""

    def __init__(self, inner: FixtureLLMBackend, n: int) -> None:
        self.inner, self.n, self.count = inner, n, 0

    def complete(self, **kw: Any) -> LLMResult:
        self.count += 1
        if self.count == self.n:
            raise TimeoutError("slow")
        return self.inner.complete(**kw)


def test_backend_error_in_one_section_fails_the_whole_act_and_retries_next_crawl(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _act_settings(tmp_path)
    replies = [
        _whole_act_reply(),
        _reply(_section_payload([{"text": "Keep contract records.", "quote": ACT_QUOTE_1}])),
        _reply(_section_payload([{"text": "Offer a withdrawal function.", "quote": ACT_QUOTE_2}])),
        _reply(_section_payload([])),
    ]
    inner = FixtureLLMBackend(_fixture_dir(tmp_path, ACT_URL, replies))
    engine = make_engine(settings)
    with Session(engine) as s, caplog.at_level(logging.WARNING):
        s.add(_act_row())
        s.commit()
        # call 3 = section "Article 1, point (2)"
        summary = normalize_all(s, backend=_FailOnCall(inner, 3), settings=settings)
        assert summary.n_docs == 1 and summary.n_failed == 1 and summary.n_ok == 0
        assert summary.n_changes_new == 0
        assert s.exec(select(RegChangeRow)).all() == []
        assert [d.url for d in documents_needing_normalize(s)] == [ACT_URL]
    assert any(
        "backend:TimeoutError" in r.getMessage() and ACT_URL in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize(
    ("title", "short"),
    [
        (
            "Directive (EU) 2023/2673 of the European Parliament and of the Council of 22 November "
            "2023 amending Directive 2011/83/EU as regards financial services contracts concluded "
            "at a distance and repealing Directive 2002/65/EC",
            "Directive (EU) 2023/2673",
        ),
        (
            "REGULATION (EU) 2023/1115 OF THE EUROPEAN PARLIAMENT AND OF THE COUNCIL",
            "Regulation (EU) 2023/1115",
        ),
        (
            "COMMISSION IMPLEMENTING REGULATION (EU) 2025/2650 of 1 December 2025 laying down",
            "Commission Implementing Regulation (EU) 2025/2650",
        ),
        (
            "Commission delegated decision (EC) No 12/2009 on x",
            "Commission Delegated Decision (EC) No 12/2009",
        ),
        ("Guidance note of the Board on online sales", "Guidance note"),
        ("x" * 200, "x" * 80),
    ],
)
def test_short_act_name(title: str, short: str) -> None:
    assert norm.short_act_name(title) == short


def test_section_instruments_use_the_short_act_name(tmp_path: Path) -> None:
    settings = _act_settings(tmp_path)
    whole = _payload(
        [],
        instrument="Directive (EU) 2099/1 of the European Parliament and of the Council of 1 May "
        "2099 amending Directive 2011/83/EU as regards distance contracts",
        effective_dates=["2026-06-19"],
    )
    replies = [
        _reply(whole),
        _reply(_section_payload([{"text": "Keep records.", "quote": ACT_QUOTE_1}])),
        _reply(_section_payload([])),
        _reply(_section_payload([])),
    ]
    backend = FixtureLLMBackend(_fixture_dir(tmp_path, ACT_URL, replies))
    result = normalize_act(
        _act_row().to_schema(),
        backend=backend,
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    assert [c.instrument for c in result.changes] == [
        "Directive (EU) 2099/1 — Article 1, point (1)"
    ]
