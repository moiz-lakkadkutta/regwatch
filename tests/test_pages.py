"""Feed + change detail rendered from stored rows (Oct 21). Templates display stored data only."""

from __future__ import annotations

import importlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from regwatch.config import REGULATION_LABELS, SEED_REGULATIONS
from regwatch.db import (
    ApplicabilityRow,
    DocumentRow,
    RegChangeRow,
    regulation_tag_for,
    session,
)

SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"

IHK_URL = "https://www.ihk.de/eudr-kmu"
BMJ_URL = "https://www.bmj.de/widerrufsbutton-faq"
SHOPIFY_URL = "https://help.shopify.com/en/manual/us-returns"
GII_URL = "https://www.gesetze-im-internet.de/bgb/__356a.html"
UNCLEAR_REASON = (
    "We couldn't tell from your profile: do you import the goods yourself "
    "or buy from an EU wholesaler?"
)

# (url, domain, tag, instrument, summary, obligation, decision | None, reasons, created_at)
EXTRA_ROWS: list[dict[str, Any]] = [
    {
        "url": IHK_URL,
        "domain": "ihk.de",
        "tag": "eudr",
        "instrument": "IHK guidance — EUDR for small and micro enterprises",
        "summary": "The IHK explains which small businesses count as operators under the EUDR.",
        "obligation": (
            "Check whether you are an operator.",
            "Operators must submit a due diligence statement before placing relevant products "
            "on the market.",
        ),
        "decision": "unclear",
        "reasons": [UNCLEAR_REASON],
        "created_at": datetime(2026, 7, 3, 9, 0),
    },
    {
        "url": BMJ_URL,
        "domain": "bmj.de",
        "tag": "withdrawal",
        "instrument": "BMJ FAQ — Widerrufsbutton (§ 356a BGB)",
        "summary": "The ministry's FAQ on the withdrawal button for German online shops.",
        "obligation": (
            "Show the button on the shop page.",
            "Der Widerrufsbutton muss auf der Website des Unternehmers gut sichtbar sein.",
        ),
        "decision": None,
        "reasons": [],
        "created_at": datetime(2026, 7, 4, 9, 0),
    },
    {
        "url": SHOPIFY_URL,
        "domain": "help.shopify.com",
        "tag": "withdrawal",
        "instrument": "Shopify Help — returns for US customers",
        "summary": "How to configure returns for customers in the United States.",
        "obligation": (
            "Configure a return policy.",
            "You can set a return policy for orders placed by customers in the United States.",
        ),
        "decision": "does_not",
        "reasons": ["This page covers US returns; you sell to EU consumers only."],
        "created_at": datetime(2026, 7, 5, 9, 0),
    },
    {
        "url": GII_URL,
        "domain": "gesetze-im-internet.de",
        "tag": "withdrawal",
        "instrument": "§ 356a BGB — Widerrufsfunktion bei Fernabsatzverträgen",
        "summary": "German transposition of the withdrawal function: label the button, two steps.",
        "obligation": (
            "Label the button 'Vertrag hier widerrufen'.",
            "Die Widerrufsfunktion ist mit den Wörtern „Vertrag hier widerrufen“ zu beschriften.",
        ),
        "decision": "applies",
        "reasons": ["You are established in Germany, so § 356a BGB is the rule that binds you."],
        "created_at": datetime(2026, 7, 1, 9, 0),
    },
]


def _reload_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Any:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / name}")
    monkeypatch.delenv("REGWATCH_REPORT_EMAIL", raising=False)
    import regwatch.app as appmod

    return importlib.reload(appmod)


@pytest.fixture()
def seed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The recorded seed run only (two applies changes on eur-lex)."""
    appmod = _reload_app(tmp_path, monkeypatch, "seed.db")
    from regwatch.jobs.crawl import seed

    assert seed() == 2
    return TestClient(appmod.app)


@pytest.fixture()
def empty_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    appmod = _reload_app(tmp_path, monkeypatch, "empty.db")
    return TestClient(appmod.app)


@pytest.fixture()
def pages_client(seed_client: TestClient) -> TestClient:
    """Seed run + one unclear, one does_not, one applies-without-todos and one unchecked change."""
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        prof = appmod.current_profile(s)
        assert prof is not None and prof.id is not None
        for spec in EXTRA_ROWS:
            text, quote = spec["obligation"]
            s.add(
                DocumentRow(
                    url=spec["url"],
                    content_hash=f"hash-{spec['domain']}",
                    source_domain=spec["domain"],
                    published_date="2026-06-15",
                    fetched_at=datetime(2026, 7, 1, 8, 0),
                    markdown=f"# {spec['instrument']}\n\n{quote}\n",
                    regulation_tags=json.dumps([spec["tag"]]),
                )
            )
            row = RegChangeRow(
                document_url=spec["url"],
                content_hash=f"hash-{spec['domain']}",
                model="nvidia/Nemotron-3_5-Lightning",
                instrument=spec["instrument"],
                jurisdiction="DE" if spec["domain"].endswith(".de") else "EU",
                summary=spec["summary"],
                effective_dates="[]",
                obligations=json.dumps([{"text": text, "quote": quote}]),
                created_at=spec["created_at"],
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            assert row.id is not None
            if spec["decision"] is not None:
                s.add(
                    ApplicabilityRow(
                        profile_id=prof.id,
                        regchange_id=row.id,
                        decision=spec["decision"],
                        reasons=json.dumps(spec["reasons"]),
                        todos="[]",
                        model="nvidia/Nemotron-3-Ultra-550b-a55b",
                        cost_usd=0.01,
                    )
                )
                s.commit()
    return seed_client


def _change_id(url: str) -> int:
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        row = s.exec(select(RegChangeRow).where(RegChangeRow.document_url == url)).one()
    assert row.id is not None
    return row.id


def _section(html: str, key: str) -> str:
    start = html.index(f'<section class="group" id="{key}">')
    end = html.index("</section>", start)
    return html[start:end]


# ---------- helpers (plan §5 test 13) ----------


def test_helpers() -> None:
    from regwatch.app import FeedItem, _group, fmt_date, fmt_stamp, wrong_call_mailto
    from regwatch.schemas import RegChange

    assert fmt_date(date(2026, 6, 19)) == "19 Jun 2026"
    assert fmt_date(datetime(2026, 6, 1, 9, 0)) == "1 Jun 2026"
    assert fmt_date(None) == ""
    assert fmt_stamp(datetime(2026, 10, 21, 14, 2)) == "21 Oct 2026 14:02 UTC"
    assert fmt_stamp(None) == ""
    assert (
        wrong_call_mailto(1, "applies")
        == "mailto:?subject=RegWatch%20wrong%20call%3A%20change%20%231%20is%20marked%20applies"
    )
    assert wrong_call_mailto(2, "unclear", "ops@example.org").startswith("mailto:ops@example.org?")

    doc = DocumentRow(
        url="https://x",
        content_hash="h",
        source_domain="ihk.de",
        markdown="",
        regulation_tags=json.dumps(["eudr", "withdrawal"]),
    )
    assert regulation_tag_for(doc) == "withdrawal"  # SEED_REGULATIONS order wins over list order
    assert regulation_tag_for(None) is None
    doc.regulation_tags = json.dumps(["something-else"])
    assert regulation_tag_for(doc) is None

    change = RegChange(
        document_url="https://www.bmj.de/x",
        instrument="Some untagged act",
        jurisdiction="DE",
        summary="s",
        obligations=[],
    )
    untagged = FeedItem(
        id=9,
        change=change,
        decision="applies",
        reasons=[],
        todos=[],
        created_at=datetime(2026, 7, 1),
        tag=None,
        source_domain="bmj.de",
        next_due=None,
        in_force=False,
        published_date=None,
        fetched_at=None,
        content_hash="",
        normalize_model="m",
        classify_model="c",
        cost_usd=0.0,
    )
    tagged = FeedItem(
        id=8,
        change=change,
        decision="unclear",
        reasons=[],
        todos=[],
        created_at=datetime(2026, 6, 1),
        tag="eudr",
        source_domain="bmj.de",
        next_due=None,
        in_force=False,
        published_date=None,
        fetched_at=None,
        content_hash="",
        normalize_model="m",
        classify_model="c",
        cost_usd=0.0,
    )
    groups = _group([untagged, tagged])
    assert [g.key for g in groups] == ["eudr", "Some untagged act"]
    assert groups[0].label == REGULATION_LABELS["eudr"]
    assert groups[0].instrument == SEED_REGULATIONS[1]["instrument"]
    assert groups[1].label == "Some untagged act" and groups[1].instrument is None


def test_feed_rows_and_unchecked_from_db(pages_client: TestClient) -> None:
    import regwatch.app as appmod
    from regwatch.db import corpus_counts, feed_row, feed_rows, unchecked_changes

    with session(appmod.engine) as s:
        prof = appmod.current_profile(s)
        assert prof is not None and prof.id is not None
        rows = feed_rows(s, prof.id)
        assert [r.source_domain for r in rows] == [
            "help.shopify.com",
            "ihk.de",
            "gesetze-im-internet.de",
            "eur-lex.europa.eu",
            "eur-lex.europa.eu",
        ]
        assert {r.tag for r in rows} == {"withdrawal", "eudr"}
        one = feed_row(s, prof.id, 1)
        assert one is not None and one.tavily_credits == 6 and one.normalize_model == "seed"
        assert feed_row(s, prof.id, _change_id(BMJ_URL)) is None
        assert [c.document_url for c in unchecked_changes(s, prof.id)] == [BMJ_URL]
        assert corpus_counts(s) == (6, 5)
