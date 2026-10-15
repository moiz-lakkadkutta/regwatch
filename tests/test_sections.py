"""Section splitter: real Directive 2023/2673 fixture plus synthetic edge cases. Offline."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from regwatch.config import Settings
from regwatch.schemas import Document
from regwatch.understand.sections import INTRO_CAP, _pack, is_long_act, split_act

FIXTURE = Path(__file__).parent / "fixtures" / "acts" / "dir-2023-2673.md"
INTRO = (
    "Article 1\n\nAmendments to Directive 2011/83/EU\n\nDirective 2011/83/EU is amended as follows:"
)


@pytest.fixture(scope="module")
def act() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _strip_intro(text: str) -> str:
    return text[len(INTRO) :] if text.startswith(INTRO) else text


def _ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_fixture_point_three_is_its_own_section(act: str) -> None:
    sections = {s.label: s.text for s in split_act(act)}
    text = sections["Article 1, point (3)"]
    assert "Article 11a" in text
    assert "withdraw from contract here" in text
    assert "Directive 2011/83/EU is amended as follows" in text


def test_fixture_size_bound_and_verbatim(act: str) -> None:
    for s in split_act(act, max_chars=15_000):
        assert len(s.text) <= 15_000 + INTRO_CAP
        rest = _strip_intro(s.text).strip()
        assert rest in act, s.label


def test_fixture_covers_operative_body(act: str) -> None:
    body = act.split("HAVE ADOPTED THIS DIRECTIVE:", 1)[1]
    joined = INTRO + "".join(_strip_intro(s.text) for s in split_act(act))
    assert _ws(joined) == _ws(body)


def test_no_have_adopted_splits_whole_text() -> None:
    md = "Preamble words.\n\nArticle 1\n\nFirst.\n\nArticle 2\n\nSecond.\n"
    sections = split_act(md, min_chars=1, max_chars=1000)
    assert [s.label for s in sections] == ["Introductory provisions", "Article 1", "Article 2"]


def test_small_articles_merge_with_en_dash() -> None:
    md = "HAVE ADOPTED THIS REGULATION:\n\nArticle 1\n\nA.\n\nArticle 2\n\nB.\n\nArticle 3\n\nC.\n"
    sections = split_act(md, min_chars=100, max_chars=1000)
    assert [s.label for s in sections] == ["Article 1 – Article 3"]
    assert "B." in sections[0].text


def test_oversize_article_is_split_into_parts() -> None:
    paras = "\n\n".join(f"Paragraph {i} " + "x" * 80 for i in range(20))
    md = f"HAVE ADOPTED THIS DECISION:\n\nArticle 1\n\n{paras}\n"
    sections = split_act(md, min_chars=10, max_chars=500)
    assert len(sections) > 2
    assert [s.label for s in sections][:2] == ["Article 1 (part 1)", "Article 1 (part 2)"]
    assert all(len(s.text) <= 500 for s in sections)
    assert _ws("".join(s.text for s in sections)) == _ws(f"Article 1\n\n{paras}")


def test_single_huge_paragraph_is_hard_cut() -> None:
    md = "Article 1\n\n" + "y" * 2500
    sections = split_act(md, min_chars=10, max_chars=1000)
    assert all(len(s.text) <= 1000 for s in sections)
    assert _ws("".join(s.text for s in sections)) == _ws(md)


def test_oversize_point_keeps_intro_within_bound() -> None:
    paras = "\n\n".join("z" * 300 for _ in range(10))
    md = (
        "Article 1\n\nDirective X is amended as follows:\n\n"
        f"(1)the following Article is inserted: {paras}\n\n(2)in Article 5, point (a) is deleted;\n"
    )
    sections = split_act(md, min_chars=10, max_chars=1000)
    labels = [s.label for s in sections]
    assert "Article 1, point (1) (part 1)" in labels
    assert labels[-1] == "Article 1, point (2)"
    assert all(len(s.text) <= 1000 + INTRO_CAP for s in sections)
    assert all(s.text.startswith("Article 1\n\nDirective X is amended") for s in sections)


def test_blank_input_has_no_sections() -> None:
    assert split_act("  \n") == []


@pytest.mark.parametrize(
    ("url", "size", "expected"),
    [
        ("https://eur-lex.europa.eu/eli/dir/2023/2673/oj", 100_000, True),
        ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32023L2673", 100_000, True),
        ("https://eur-lex.europa.eu/eli/dir/2023/2673/oj", 5_000, False),
        ("https://help.shopify.com/en/manual/x", 100_000, False),
        # guidance summaries of an act are never the act itself
        (
            "https://eur-lex.europa.eu/EN/legal-content/summary/"
            "fighting-deforestation-and-forest-degradation.html",
            100_000,
            False,
        ),
        ("https://eur-lex.europa.eu/legal-content/summary/withdrawal.html", 100_000, False),
    ],
)
def test_is_long_act(url: str, size: int, expected: bool) -> None:
    doc = Document(
        url=url,
        content_hash="h",
        fetched_at=datetime.now(UTC),
        markdown="a" * size,
        source_domain=("eur-lex.europa.eu" if "eur-lex" in url else "help.shopify.com"),
    )
    assert is_long_act(doc, Settings()) is expected


def test_pack_never_emits_blank_parts() -> None:
    assert _pack("a" * 10 + "\n\n" + "b" * 9, 10) == ["a" * 10, "b" * 9]
    assert _pack("a" * 10 + "\n\n" + "b" * 10 + "\n\n" + "c", 10) == ["a" * 10, "b" * 10, "c"]


def test_split_act_paragraphs_near_max_have_no_blank_sections() -> None:
    paras = "\n\n".join(chr(97 + i) * 98 for i in range(8))
    md = (
        "Article 1\n\nDirective X is amended as follows:\n\n"
        f"(1)the following Article is inserted: {paras}\n\n(2)in Article 5, point (a) is deleted;\n"
    )
    for max_chars in (100, 101, 102, 200):
        for s in split_act(md, min_chars=10, max_chars=max_chars):
            assert s.text.strip()
            assert not s.text.strip().endswith("amended as follows:"), s.label


@pytest.mark.parametrize(
    "points",
    ["(2)in Article 5, x;\n\n(3)in Article 6, y;", "(1)in Article 5, x;\n\n(3)in Article 6, y;"],
)
def test_out_of_order_points_keep_article_whole(points: str) -> None:
    md = f"Article 1\n\nDirective X is amended as follows:\n\n{points}\n"
    sections = split_act(md, min_chars=1, max_chars=1000)
    assert [s.label for s in sections] == ["Article 1"]
    assert _ws(sections[0].text) == _ws(md)


def test_commission_act_has_adopted_this_regulation_skips_recitals() -> None:
    md = (
        "Whereas:\n\n(1) Recital text that must never be normalised.\n\n"
        "HAS ADOPTED THIS REGULATION:\n\nArticle 1\n\nA.\n\nArticle 2\n\nB.\n"
    )
    sections = split_act(md, min_chars=1, max_chars=1000)
    assert [s.label for s in sections] == ["Article 1", "Article 2"]
    assert all("Recital text" not in s.text for s in sections)


def test_blank_operative_body_falls_back_to_whole_markdown() -> None:
    md = "Preamble words that matter.\n\nHAVE ADOPTED THIS DIRECTIVE:\n"
    sections = split_act(md, min_chars=1, max_chars=1000)
    assert sections and all(s.text.strip() for s in sections)
    assert "Preamble words that matter." in sections[0].text


def test_nbsp_headings_split_per_article() -> None:
    # ELI pages of newer acts (e.g. Regulation (EU) 2025/40, live crawl 2026-10-15) write
    # "Article\xa01" and "ANNEX\xa0I"; without this every article ends up in "Introductory
    # provisions".
    body = "\n\n".join(f"Article\xa0{i}\n\nTitle {i}\n\n" + "z" * 3200 for i in (1, 2))
    md = f"HAVE ADOPTED THIS REGULATION:\n\nCHAPTER\xa0I\n\n{body}\n\nANNEX\xa0I\n\nList.\n"
    labels = [s.label for s in split_act(md)]
    assert labels == ["Introductory provisions", "Article 1", "Article 2", "ANNEX I"]


def test_headless_annex_of_amending_act_is_not_glued_to_last_article() -> None:
    # The ELI page of Directive (EU) 2024/825 (live crawl 2026-10-15) drops the "ANNEX" heading;
    # its annex starts "Annex I to Directive 2005/29/EC is amended as follows:" after the last
    # article ("Addressees"). It must not be labelled "Article 6".
    md = (
        "HAVE ADOPTED THIS DIRECTIVE:\n\nArticle 1\n\n" + "a" * 3200 + "\n\nArticle 6\n\n"
        "Addressees\n\nThis Directive is addressed to the Member States.\n\n---\n\n"
        "Annex I to Directive 2005/29/EC is amended as follows:\n\n" + "b" * 3200 + "\n"
    )
    sections = split_act(md)
    assert [s.label for s in sections] == ["Article 1", "Article 6", "ANNEX"]
    assert sections[2].text.startswith("Annex I to Directive 2005/29/EC is amended as follows:")


def test_annex_heading_followed_by_amendment_opener_stays_one_section() -> None:
    md = (
        "HAVE ADOPTED THIS DIRECTIVE:\n\nArticle 1\n\n" + "a" * 3200 + "\n\nANNEX I\n\n"
        "Annex I to Directive 2011/83/EU is amended as follows:\n\n" + "b" * 3200 + "\n"
    )
    assert [s.label for s in split_act(md)] == ["Article 1", "ANNEX I"]
