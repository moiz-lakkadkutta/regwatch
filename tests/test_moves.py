"""'This rule moved' banners: grounded in code, seeded idempotently, shown on EUDR items only."""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlmodel import select

from regwatch.config import Settings
from regwatch.db import DocumentRow, make_engine, session
from regwatch.jobs.crawl import read_documents_jsonl
from regwatch.moves import RuleMove, RuleMoveRow, matching, read_moves, seed_moves
from regwatch.schemas import Profile, RegChange
from tests.conftest import ART_11A, EUDR_URL, SEED_DIR, seed_change_id

EUDR_DOC = next(d for d in read_documents_jsonl(SEED_DIR / "documents.jsonl") if d.url == EUDR_URL)
MOVES = read_moves(SEED_DIR / "rule-moves.json")
SMALL = next(m for m in MOVES if m.company_size == "small")
EUDR_APPLIES = "Regulation (EU) 2023/1115 — Article 3"
EUDR_DOES_NOT = "Regulation (EU) 2023/1115 — Article 25"
BANNER = "Deadline moved: now 30 June 2027 for small companies"


def _profile(size: str | None) -> Profile:
    return Profile.model_validate(
        {"establishment_country": "DE", "ship_to": ["DE"], "categories": [], "company_size": size}
    )


def test_seed_moves_are_grounded_in_the_seed_document() -> None:
    assert {m.company_size for m in MOVES} == {"small", "larger"}
    for m in MOVES:
        assert m.document_url == EUDR_URL
        assert m.validate_quote(EUDR_DOC.markdown) == []
    assert "30 June 2027" in SMALL.quote and SMALL.amending_act == "Regulation (EU) 2025/2650"


@pytest.mark.parametrize(
    ("update", "problem"),
    [
        ({"quote": SMALL.quote.replace("30 June 2027", "30 June 2028")}, "not found verbatim"),
        (
            {"quote": "Regulation (EU) No 995/2010 is repealed with effect from 30 December 2026."},
            "does not state the new date",
        ),
        ({"amending_act": "Regulation (EU) 2025/9999"}, "does not name the amending act"),
        ({"amending_url": "https://eur-lex.europa.eu/eli/reg/2025/9999/oj"}, "does not link"),
        ({"citation_url": "https://example.com/eudr"}, "not on config.SOURCE_DOMAINS"),
        ({"old_date": date(2028, 1, 1)}, "old_date must be before new_date"),
    ],
)
def test_validate_quote_refuses_ungrounded_moves(update: dict[str, Any], problem: str) -> None:
    bad = SMALL.model_copy(update=update)
    problems = bad.validate_quote(EUDR_DOC.markdown)
    assert any(problem in p for p in problems), problems


def test_seed_moves_refuses_an_ungrounded_quote_and_stores_nothing(tmp_path: Path) -> None:
    engine = make_engine(Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / 'm.db'}"))
    bad = SMALL.model_copy(update={"quote": "EUDR is postponed to 30 June 2027 for small shops."})
    with session(engine) as s:
        with pytest.raises(ValueError, match="not stored"):
            seed_moves(s, [SMALL])  # no source document yet
        s.add(
            DocumentRow(
                url=EUDR_DOC.url,
                content_hash=EUDR_DOC.content_hash,
                source_domain=EUDR_DOC.source_domain,
                markdown=EUDR_DOC.markdown,
            )
        )
        s.commit()
        with pytest.raises(ValueError, match="not found verbatim"):
            seed_moves(s, [bad])
        assert s.exec(select(RuleMoveRow)).all() == []
        assert seed_moves(s, [SMALL]) == 1
        assert seed_moves(s, [SMALL]) == 0


def test_matching_needs_the_act_the_new_date_and_a_relevant_decision() -> None:
    change = RegChange.model_validate(
        {
            "document_url": EUDR_URL,
            "instrument": EUDR_APPLIES,
            "jurisdiction": "EU",
            "summary": "s",
            "effective_dates": ["2026-12-30", "2027-06-30"],
            "obligations": [],
        }
    )
    assert matching(MOVES, change, "applies", []) == MOVES
    assert matching(MOVES, change, "does_not", []) == []
    other = change.model_copy(
        update={"instrument": ART_11A, "effective_dates": [date(2027, 6, 30)]}
    )
    assert matching(MOVES, other, "applies", []) == []
    assert SMALL.for_profile(_profile("small")) and SMALL.for_profile(_profile(None))
    assert not SMALL.for_profile(_profile("larger"))


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'seed.db'}")
    import regwatch.app as appmod

    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_today", lambda: date(2026, 10, 15))
    from regwatch.jobs.crawl import seed

    seed()
    return TestClient(appmod.app)


def test_seed_is_idempotent_with_moves(client: TestClient) -> None:
    from regwatch.jobs.crawl import seed

    settings = Settings()
    assert seed(settings) == 0
    with session(make_engine(settings)) as s:
        rows = s.exec(select(RuleMoveRow)).all()
    assert sorted(r.key for r in rows) == sorted(m.key for m in MOVES)
    assert [RuleMove.model_validate_json(r.payload) for r in rows if r.key == SMALL.key] == [SMALL]


def _section(html: str, key: str) -> str:
    return html.split(f'<section class="group" id="{key}">')[1].split("</section>")[0]


def test_banner_on_eudr_feed_group_not_on_withdrawal(client: TestClient) -> None:
    html = client.get("/feed").text
    eudr, withdrawal = _section(html, "eudr"), _section(html, "withdrawal")
    assert eudr.count(BANNER) == 1  # once per group, not per item
    assert "It was 30 June 2026." in eudr
    assert str(escape(SMALL.quote)) in eudr
    assert 'href="https://eur-lex.europa.eu/legal-content/EN/AUTO/?uri=celex:32025R2650"' in eudr
    assert "medium and large companies" not in html  # the demo store is small
    assert "Deadline moved" not in withdrawal


def test_banner_on_eudr_detail_not_on_withdrawal_or_does_not(client: TestClient) -> None:
    detail = client.get(f"/change/{seed_change_id(EUDR_APPLIES)}").text
    assert BANNER in detail and str(escape(SMALL.quote)) in detail
    assert f'href="{SMALL.citation_url}"' in detail
    assert "Deadline moved" not in client.get(f"/change/{seed_change_id(ART_11A)}").text
    assert "Deadline moved" not in client.get(f"/change/{seed_change_id(EUDR_DOES_NOT)}").text


def test_banner_in_month_view(client: TestClient) -> None:
    html = client.get("/month").text
    assert html.count(BANNER) == 1
