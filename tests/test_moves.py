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

DOCS = {d.url: d for d in read_documents_jsonl(SEED_DIR / "documents.jsonl")}
EUDR_DOC = DOCS[EUDR_URL]
MOVES = read_moves(SEED_DIR / "rule-moves.json")
SMALL = next(m for m in MOVES if m.company_size == "small")
LARGER = next(m for m in MOVES if m.company_size == "larger")
WOOD = next(ex for ex in SMALL.exceptions if ex.categories == ["furniture_wood"])
EUTR_URL = "https://eur-lex.europa.eu/eli/reg/2010/995/oj/eng"
AMENDING_TEXT_URL = "https://eur-lex.europa.eu/eli/reg/2025/2650/oj"
OLD_ACT_URL = "https://eur-lex.europa.eu/eli/reg/2024/3234/oj/eng"
EUDR_APPLIES = "Regulation (EU) 2023/1115 — Article 3"
EUDR_DOES_NOT = "Regulation (EU) 2023/1115 — Article 25"
BANNER = "Deadline moved: now 30 June 2027 for small companies"
# The default store is small and sells wooden furniture and leather goods (Art. 38(3) EUDR leaves
# out the products in the Annex to Regulation (EU) No 995/2010, which lists wooden furniture).
WOOD_LINE = "Wooden furniture: due 30 December 2026, not moved to 30 June 2027"
LEATHER_LINE = "Leather goods: moved to 30 June 2027 for small companies"


def _markdown_for(url: str) -> str | None:
    doc = DOCS.get(url)
    return None if doc is None else doc.markdown


def _profile(size: str | None, categories: list[str] | None = None) -> Profile:
    return Profile.model_validate(
        {
            "establishment_country": "DE",
            "ship_to": ["DE"],
            "categories": categories or [],
            "company_size": size,
        }
    )


def test_seed_moves_are_grounded_in_the_seed_documents() -> None:
    assert {m.company_size for m in MOVES} == {"small", "larger"}
    for m in MOVES:
        assert m.document_url == EUDR_URL
        assert m.validate_quote(EUDR_DOC.markdown) == []
        assert m.validate_sources(_markdown_for) == []
        assert m.amending_text_url == AMENDING_TEXT_URL  # the amending act states the date too
        assert m.old_citation_url == OLD_ACT_URL and m.old_act == "Regulation (EU) 2024/3234"
    assert "30 June 2027" in SMALL.quote and SMALL.amending_act == "Regulation (EU) 2025/2650"
    assert SMALL.quote.startswith("Except as regards the products covered by the Annex to")
    assert "30 June 2026" in SMALL.old_quote and "30 December 2025" in LARGER.old_quote
    # the exception: wooden furniture keeps the Art. 38(2) date
    assert WOOD.citation_url == EUTR_URL and "Wooden furniture" in WOOD.quote
    assert WOOD.due == date(2026, 12, 30) and WOOD.due_quote == LARGER.quote
    assert [ex.products for ex in SMALL.exceptions] == ["wooden furniture", "paper"]
    assert LARGER.exceptions == []


@pytest.mark.parametrize(
    ("update", "problem"),
    [
        ({"old_quote": SMALL.old_quote.replace("30 June 2026", "30 June 2025")}, "not found"),
        ({"old_date": date(2026, 7, 1)}, "old_quote does not state the old date"),
        ({"old_citation_url": ""}, "go together"),
        ({"old_citation_url": "https://eur-lex.europa.eu/eli/reg/2099/1/oj"}, "not stored"),
        ({"amending_text_url": OLD_ACT_URL}, "not found verbatim"),
        ({"categories": ["leather"]}, "categories not in the move's"),
        ({"categories": [*SMALL.categories, "timber"]}, "unknown categories"),
        (
            {
                "exceptions": [
                    WOOD.model_copy(update={"quote": "9403 Wooden furniture of all kinds"})
                ]
            },
            "not found verbatim",
        ),
        ({"exceptions": [WOOD.model_copy(update={"due": date(2026, 12, 31)})]}, "does not state"),
        (
            {"exceptions": [WOOD.model_copy(update={"citation_url": "https://example.com/eutr"})]},
            "not on config.SOURCE_DOMAINS",
        ),
    ],
)
def test_validate_sources_refuses_ungrounded_parts(update: dict[str, Any], problem: str) -> None:
    bad = SMALL.model_copy(update=update)
    problems = bad.validate_sources(_markdown_for)
    assert any(problem in p for p in problems), problems


def test_for_store_narrows_exceptions_by_category() -> None:
    furniture = SMALL.for_store(_profile("small", ["furniture_wood", "leather"]))
    assert [ex.products for ex in furniture.exceptions] == ["wooden furniture"]
    assert furniture.moved_names == ["leather goods"]
    leather = SMALL.for_store(_profile("small", ["leather"]))
    assert leather.exceptions == [] and leather.moved_names == ["leather goods"]
    wood_only = SMALL.for_store(_profile("small", ["furniture_wood"]))
    assert len(wood_only.exceptions) == 1 and wood_only.moved_names == []
    clothing = SMALL.for_store(_profile("small", ["apparel"]))
    assert clothing.exceptions == [] and clothing.moved_names == []
    assert len(SMALL.exceptions) == 2  # the stored move is untouched


def _banner(mv: RuleMove) -> str:
    import regwatch.app as appmod

    html = appmod.templates.get_template("partials/rule_move.html").render(mv=mv)
    return " ".join(html.split())


def test_banner_wording_furniture_store_vs_leather_only_store() -> None:
    furniture = _banner(SMALL.for_store(_profile("small", ["furniture_wood", "leather"])))
    assert WOOD_LINE in furniture and LEATHER_LINE in furniture
    assert furniture.index(WOOD_LINE) < furniture.index(LEATHER_LINE)  # earliest date first
    assert BANNER not in furniture  # never "everything moved to 2027"
    assert "Leather goods were due 30 June 2026." in furniture
    eutr = "the EU Timber Regulation (Regulation (EU) No 995/2010), which lists wooden furniture"
    assert eutr in furniture
    for quote in (SMALL.quote, WOOD.quote, WOOD.due_quote, SMALL.old_quote):
        assert " ".join(str(escape(quote)).split()) in furniture
    assert 'href="' + EUTR_URL + '"' in furniture and 'href="' + OLD_ACT_URL + '"' in furniture
    assert "Pulp and paper" not in furniture  # the store sells no paper

    leather = _banner(SMALL.for_store(_profile("small", ["leather"])))
    assert BANNER in leather and "It was 30 June 2026." in leather
    assert "furniture" not in leather.lower() and EUTR_URL not in leather

    wood_only = _banner(SMALL.for_store(_profile("small", ["furniture_wood"])))
    assert WOOD_LINE in wood_only and "moved to 30 June 2027 for" not in wood_only


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
        with pytest.raises(ValueError, match="is not stored"):
            seed_moves(s, [SMALL])  # the old act and the exception's annex are stored too
        for url in SMALL.source_urls[1:]:
            doc = DOCS[url]
            s.add(
                DocumentRow(
                    url=doc.url,
                    content_hash=doc.content_hash,
                    source_domain=doc.source_domain,
                    markdown=doc.markdown,
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
    eudr = " ".join(eudr.split())
    assert eudr.count(WOOD_LINE) == 1 and eudr.count(LEATHER_LINE) == 1  # once per group
    assert BANNER not in eudr  # the default store sells wooden furniture: not all moved
    assert "Leather goods were due 30 June 2026." in eudr
    assert str(escape(SMALL.quote)) in eudr and str(escape(WOOD.quote)) in eudr
    assert 'href="https://eur-lex.europa.eu/legal-content/EN/AUTO/?uri=celex:32025R2650"' in eudr
    assert "medium and large companies" not in html  # the demo store is small
    assert "Deadline moved" not in withdrawal


def test_banner_on_eudr_detail_not_on_withdrawal_or_does_not(client: TestClient) -> None:
    detail = " ".join(client.get(f"/change/{seed_change_id(EUDR_APPLIES)}").text.split())
    assert WOOD_LINE in detail and LEATHER_LINE in detail and str(escape(SMALL.quote)) in detail
    assert f'href="{SMALL.citation_url}"' in detail
    assert "Deadline moved" not in client.get(f"/change/{seed_change_id(ART_11A)}").text
    assert "Deadline moved" not in client.get(f"/change/{seed_change_id(EUDR_DOES_NOT)}").text


def test_banner_in_month_view(client: TestClient) -> None:
    html = " ".join(client.get("/month").text.split())
    assert html.count(WOOD_LINE) == 1 and html.count(LEATHER_LINE) == 1
    assert 'due <time datetime="2026-12-30">' in html  # wooden furniture
    assert 'due <time datetime="2027-06-30">' in html  # leather goods
