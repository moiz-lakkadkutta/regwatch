import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from regwatch.config import SOURCE_DOMAINS, Settings
from regwatch.db import DocumentRow, make_engine
from regwatch.ingest.diff import content_hash, upsert_documents
from regwatch.ingest.extract import EXTRACT_BATCH, batched, extract_documents
from regwatch.ingest.search import Hit, search_all
from regwatch.ingest.tavily_backend import CreditLedger, FixtureTavilyBackend, normalize_url
from regwatch.schemas import Document, RegChange
from tests.fakes import RecordingBackend

SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"
FETCHED = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
UNKNOWN = "https://eur-lex.europa.eu/eli/dir/2023/0/oj"
TRUSTEDSHOPS = "https://www.trustedshops.de/ratgeber/widerrufsbutton/"


def _hit(url: str, tags: tuple[str, ...] = ("withdrawal",)) -> Hit:
    return Hit(
        url=url,
        title="t",
        published_date=None,
        source_domain="eur-lex.europa.eu",
        regulation_tags=tags,
    )


def _all_docs(fake_backend: RecordingBackend) -> list[Document]:
    ledger = CreditLedger()
    hits = search_all(fake_backend, ledger=ledger)
    return extract_documents(fake_backend, hits, ledger=ledger, fetched_at=FETCHED)


def test_extract_batches_at_most_20(fake_backend: RecordingBackend) -> None:
    assert EXTRACT_BATCH == 20
    assert batched(list("abcde"), 2) == [["a", "b"], ["c", "d"], ["e"]]
    hits = [_hit(f"https://eur-lex.europa.eu/eli/dir/2023/{i}/oj") for i in range(45)]
    ledger = CreditLedger()
    extract_documents(fake_backend, hits, ledger=ledger)
    sizes = [len(kw["urls"]) for kw in fake_backend.extract_calls]
    assert sizes == [20, 20, 5]
    for kw in fake_backend.extract_calls:
        assert kw["extract_depth"] == "advanced"
        assert kw["format"] == "markdown"
        assert kw["include_usage"] is True
    assert ledger.n_extract_calls == 3
    assert ledger.credits == 8 + 8 + 2


class _OffListBackend:
    """Returns one unknown URL as failed and injects an off-list result (simulated leak)."""

    def search(self, **kw: Any) -> dict[str, Any]:
        raise AssertionError("not used")

    def extract(self, **kw: Any) -> dict[str, Any]:
        return {
            "results": [
                {
                    "url": "https://www.trustedshops.de/ratgeber/widerrufsbutton/",
                    "raw_content": "# leak",
                },
                {"url": kw["urls"][0], "raw_content": "# ok body"},
            ],
            "failed_results": [
                {"url": "https://eur-lex.europa.eu/eli/dir/2023/0/oj", "error": "404"}
            ],
            "usage": {"credits": 2},
        }


def test_extract_logs_failed_results_and_drops_offlist(
    fake_backend: RecordingBackend, caplog: pytest.LogCaptureFixture
) -> None:
    unknown = "https://eur-lex.europa.eu/eli/dir/2023/0/oj"
    with caplog.at_level(logging.WARNING, logger="regwatch.ingest.extract"):
        docs = extract_documents(fake_backend, [_hit(unknown)], ledger=CreditLedger())
    assert docs == []
    assert any(
        unknown in r.getMessage() and "not in fixtures" in r.getMessage() for r in caplog.records
    )

    caplog.clear()
    good = "https://eur-lex.europa.eu/eli/dir/2023/2673/oj"
    with caplog.at_level(logging.WARNING, logger="regwatch.ingest.extract"):
        docs = extract_documents(
            _OffListBackend(), [_hit(good), _hit(unknown)], ledger=CreditLedger()
        )
    assert [d.url for d in docs] == [good]
    assert any(
        "trustedshops" in r.getMessage() and "off-list" in r.getMessage() for r in caplog.records
    )
    assert any(unknown in r.getMessage() and "404" in r.getMessage() for r in caplog.records)


def test_documents_have_hash_domain_tags(
    fake_backend: RecordingBackend, manifest: list[dict[str, Any]]
) -> None:
    docs = _all_docs(fake_backend)
    assert len(docs) == 16
    by_url = {d.url: d for d in docs}
    assert len(by_url) == 16
    for d in docs:
        assert d.content_hash == content_hash(d.markdown)
        assert d.source_domain in SOURCE_DOMAINS
        assert d.fetched_at == FETCHED
        assert d.regulation_tags == sorted(d.regulation_tags) and d.regulation_tags
    gii = by_url["https://www.gesetze-im-internet.de/bgb/__356a.html"]
    assert gii.source_domain == "gesetze-im-internet.de"
    for m in manifest:
        d = by_url[normalize_url(m["url"])]
        assert d.source_domain == m["source_domain"]
        assert d.regulation_tags == m["regulation_tags"]


def test_upsert_new_changed_unchanged(fake_backend: RecordingBackend, tmp_path: Path) -> None:
    docs = _all_docs(fake_backend)
    engine = make_engine(Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'd.db'}"))
    with Session(engine) as s:
        first = upsert_documents(s, docs)
        assert len(first.new) == 16 and first.changed == [] and first.unchanged == []
        assert first.total == 16
        second = upsert_documents(s, docs)
        assert second.new == [] and second.changed == [] and len(second.unchanged) == 16
        edited = docs[0].model_copy(update={"markdown": docs[0].markdown + "\n\nAmended."})
        edited = edited.model_copy(update={"content_hash": content_hash(edited.markdown)})
        third = upsert_documents(s, [edited, *docs[1:]])
        assert third.changed == [edited.url] and len(third.unchanged) == 15 and third.new == []
        row = s.exec(select(DocumentRow).where(DocumentRow.url == edited.url)).one()
        assert row.content_hash == edited.content_hash
        assert row.markdown.endswith("Amended.")
        assert row.to_schema().regulation_tags == edited.regulation_tags
        assert json.loads(row.regulation_tags) == edited.regulation_tags


def test_core_fixtures_contain_seed_quotes(
    fixtures_root: Path, manifest: list[dict[str, Any]]
) -> None:
    backend = FixtureTavilyBackend(fixtures_root)
    by_url = {m["url"]: m for m in manifest}
    change_files = sorted(SEED_DIR.glob("change-*.json"))
    assert len(change_files) >= 2
    for path in change_files:
        change = RegChange.model_validate(json.loads(path.read_text(encoding="utf-8"))["change"])
        entry = by_url[change.document_url]
        assert entry["kind"] == "core" and entry["expected_decision"] == "applies"
        resp = backend.extract(urls=[change.document_url])
        assert resp["failed_results"] == []
        markdown = resp["results"][0]["raw_content"]
        assert change.validate_quotes(markdown) == []
    for m in manifest:
        body = (fixtures_root / "extract" / f"{m['id']}.md").read_text(encoding="utf-8")
        if m["kind"] == "core":
            assert len(body) >= 600
        else:
            assert body.strip()
