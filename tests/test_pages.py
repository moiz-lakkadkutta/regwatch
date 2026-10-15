"""Feed + change detail rendered from stored rows (Oct 21). Templates display stored data only."""

from __future__ import annotations

import importlib
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlmodel import select

from regwatch.config import REGULATION_LABELS, SEED_REGULATIONS
from regwatch.db import (
    ApplicabilityRow,
    CrawlRunRow,
    DocumentRow,
    RegChangeRow,
    regulation_tag_for,
    session,
)
from tests.conftest import (
    ART_11A,
    SEED_CHANGES,
    SEED_FILES,
    seed_change_id,
    seed_file,
    seed_payload,
)

SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"
SEED_DOCS = [
    json.loads(line)
    for line in (SEED_DIR / "documents.jsonl").read_text(encoding="utf-8").splitlines()
]
SEED_SOURCES = {d["source_domain"] for d in SEED_DOCS}
SEED_CREDITS = sum(seed_payload(p)["tavily_credits"] for p in SEED_FILES)

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
    """The recorded seed run only (sections of 2023/2673 and 2023/1115, guidance pages)."""
    appmod = _reload_app(tmp_path, monkeypatch, "seed.db")
    from regwatch.jobs.crawl import seed

    assert seed() == SEED_CHANGES
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
    assert wrong_call_mailto(3, "does_not").endswith("is%20marked%20doesn%27t%20apply")

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
        assert len(rows) == SEED_CHANGES + 3  # every extra row but the unchecked one
        stamps = [r.created_at for r in rows]
        assert stamps == sorted(stamps, reverse=True)  # newest change first
        # the recorded seed (crawled in October) comes before the July extras
        assert {r.source_domain for r in rows[:SEED_CHANGES]} == SEED_SOURCES
        assert {r.tag for r in rows} == {"withdrawal", "eudr"}
        payload = seed_payload(seed_file(ART_11A))
        one = feed_row(s, prof.id, seed_change_id(ART_11A))
        assert one is not None and one.normalize_model == "seed"
        assert one.change.instrument == ART_11A and one.appl.decision == "applies"
        assert one.classify_model == payload["model"] and one.cost_usd == payload["cost_usd"]
        assert one.tavily_credits == payload["tavily_credits"]
        assert feed_row(s, prof.id, _change_id(BMJ_URL)) is None
        assert [c.document_url for c in unchecked_changes(s, prof.id)] == [BMJ_URL]
        extra_sources = {spec["domain"] for spec in EXTRA_ROWS}
        assert corpus_counts(s) == (len(SEED_DOCS) + 4, len(SEED_SOURCES | extra_sources))


# ---------- feed (plan §5 tests 1–7) ----------


def test_feed_groups_by_regulation_with_label_and_instrument(pages_client: TestClient) -> None:
    html = pages_client.get("/feed").text
    for label in REGULATION_LABELS.values():
        assert label in html
    assert html.index('id="withdrawal"') < html.index('id="eudr"')
    for r in SEED_REGULATIONS:
        assert r["instrument"] in html
    assert "IHK guidance" in _section(html, "eudr")
    assert "Shopify Help" in _section(html, "withdrawal")


def test_feed_orders_applies_before_unclear_before_does_not(pages_client: TestClient) -> None:
    html = pages_client.get("/feed").text
    withdrawal = _section(html, "withdrawal")
    pills = [p.split("<")[0] for p in withdrawal.split('<span class="pill ')[1:]]
    pills = [p.split('"')[0] for p in pills]
    assert pills == sorted(pills, key=["applies", "unclear", "does_not"].index)
    assert pills[-1] == "does_not" and pills.count("does_not") == 1  # 2023/2673's collapse
    # newest first inside a decision: the recorded seed (6 Oct) before § 356a (1 Jul)
    titles = [h.split("</a>")[0].split(">")[-1] for h in withdrawal.split("<h3>")[1:]]
    assert ART_11A in titles[:-2]
    assert titles[-2].startswith("§ 356a BGB — Widerrufsfunktion")
    assert titles[-1].startswith("Shopify Help")
    eudr = _section(html, "eudr")
    assert eudr.index("pill applies") < eudr.index("pill unclear")
    assert "Doesn't apply to you" not in eudr


def test_feed_pills_all_three_states(pages_client: TestClient) -> None:
    html = pages_client.get("/feed").text
    assert "Applies to you" in html
    assert "Doesn't apply to you" in html
    assert "Unclear — one question" in html
    # the unchecked change carries no pill
    assert 'class="pill' not in _section(html, "unchecked")


def test_feed_item_meta(pages_client: TestClient) -> None:
    html = pages_client.get("/feed").text
    # the withdrawal button (Art. 11a): applies, four to-dos, in force since 19 June 2026
    art_11a = html.split(f'<a href="/change/{seed_change_id(ART_11A)}">{ART_11A}</a>')
    assert len(art_11a) == 2
    assert '<span class="pill applies">' in art_11a[0].rsplit("<article", 1)[1]
    assert "4 to-dos · in force since 19 Jun 2026" in art_11a[1].split("</article>")[0]
    assert "No to-dos yet — see the obligations" in html
    assert "Answer one question to find out" in html
    assert 'class="chip">eur-lex.europa.eu' in html
    assert 'class="chip">help.shopify.com' in html
    assert "Established in Germany" in html


def test_feed_footer_seed_state(seed_client: TestClient) -> None:
    html = seed_client.get("/feed").text
    assert "Showing the recorded seed run" in html
    # the real Tavily credits of the live crawl the seed was recorded from (run6)
    assert SEED_CREDITS == 40
    assert "40 Tavily credits used when it was recorded" in html
    assert "no live crawl yet" in html
    assert f"{len(SEED_DOCS)} documents from {len(SEED_SOURCES)} sources " in html
    assert "Last crawl" not in html
    assert "credits used" in html  # test_app.test_feed_shows_both_seed_changes relies on it


def test_feed_footer_after_crawl(seed_client: TestClient) -> None:
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        s.add(
            CrawlRunRow(
                started_at=datetime(2026, 10, 21, 13, 50),
                finished_at=datetime(2026, 10, 21, 14, 2),
                stage="all",
                tavily_credits=44,
                model_cost_usd=0.0312,
            )
        )
        s.commit()
    html = seed_client.get("/feed").text
    assert "Last crawl 21 Oct 2026 14:02 UTC" in html
    assert "44 Tavily credits used this crawl" in html
    assert "$0.0312 Nemotron" in html
    assert f"{len(SEED_DOCS)} documents from {len(SEED_SOURCES)} sources " in html
    assert "seed run" not in html


def test_feed_footer_unfinished_run_still_shows_seed_state(seed_client: TestClient) -> None:
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        s.add(CrawlRunRow(started_at=datetime(2026, 10, 21, 13, 50), stage="all"))
        s.commit()
    html = seed_client.get("/feed").text
    assert "Showing the recorded seed run" in html and "Last crawl" not in html


def test_feed_empty_state(empty_client: TestClient) -> None:
    empty_client.post(
        "/profile",
        data={"establishment_country": "FR", "ship_to": ["FR"], "categories": ["leather"]},
        follow_redirects=False,
    )
    html = empty_client.get("/feed").text
    assert "No changes on file yet." in html
    assert "0 documents from 0 sources" in html
    assert "Established in France" in html


def test_feed_unchecked_section_after_profile_change(pages_client: TestClient) -> None:
    r = pages_client.post(
        "/profile",
        data={"establishment_country": "AT", "ship_to": ["AT", "DE"], "categories": ["apparel"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = pages_client.get("/feed")
    assert r.status_code == 200
    html = r.text
    assert "Not checked against your profile yet" in html
    assert "regwatch crawl --stage classify" in html
    with session(__import__("regwatch.app", fromlist=["engine"]).engine) as s:
        instruments = [c.instrument for c in s.exec(select(RegChangeRow)).all()]
    assert len(instruments) == SEED_CHANGES + 4
    for name in instruments:
        assert escape(name) in html
    assert 'class="pill' not in html
    assert "Applies to you" not in html


# ---------- change detail (plan §5 tests 8–12) ----------


def _blockquotes(html: str) -> list[str]:
    return [chunk.split("</blockquote>")[0] for chunk in html.split("<blockquote>")[1:]]


def test_detail_every_todo_links_citation_url(seed_client: TestClient) -> None:
    """Grounding guard: every rendered to-do quote cites the change's own document URL."""
    import regwatch.app as appmod

    with_todos = [p for p in SEED_FILES if seed_payload(p)["applicability"]["todos"]]
    assert seed_file(ART_11A) in with_todos
    for change_id in [SEED_FILES.index(p) + 1 for p in with_todos]:
        with session(appmod.engine) as s:
            ch = s.get(RegChangeRow, change_id)
            assert ch is not None
            appl = s.exec(
                select(ApplicabilityRow).where(
                    ApplicabilityRow.regchange_id == change_id,
                    ApplicabilityRow.profile_id == 1,  # the default store the page shows
                )
            ).one()
        change = ch.to_schema()
        todos = appl.to_schema().todos
        assert todos, "seed changes carry to-dos"
        html = seed_client.get(f"/change/{change_id}").text
        for t in todos:
            assert t.citation_url == change.document_url
            assert f'<a href="{t.citation_url}"' in html
            assert escape(t.quote) in html
            assert escape(t.text) in html
        for o in change.obligations:
            assert escape(o.quote) in html
        quotes = _blockquotes(html)
        assert len(quotes) == len(todos) + len(change.obligations)
        assert all("<cite>" in q for q in quotes)
        assert all(f'href="{change.document_url}"' in q for q in quotes)


def test_templates_never_mark_stored_text_safe() -> None:
    tpl = Path(__file__).resolve().parent.parent / "regwatch" / "templates"
    for name in (
        "feed.html",
        "change_detail.html",
        "profile_form.html",
        "partials/agent_panel.html",
        "partials/agent_result.html",
    ):
        assert "|safe" not in (tpl / name).read_text(encoding="utf-8")


def test_detail_applies_todo_due_and_overdue(seed_client: TestClient) -> None:
    html = seed_client.get(f"/change/{seed_change_id(ART_11A)}").text
    assert "Applies to you" in html
    assert "Why this applies to you" in html
    assert "What to do" in html
    assert html.index("What to do") < html.index("Obligations in the text")
    assert html.index("Obligations in the text") < html.index("<h2>Source</h2>")
    # due 19 Jun 2026 is in the past on any day this test runs after that date
    assert '<span class="pill overdue">in force since 19 Jun 2026</span>' in html
    assert "19 Jun 2026" in html.split("<dt>Dates</dt>")[1].split("</dd>")[0]
    assert 'class="chip">eur-lex.europa.eu' in html


def test_detail_unclear_copy(pages_client: TestClient) -> None:
    html = pages_client.get(f"/change/{_change_id(IHK_URL)}").text
    assert "Unclear — one question" in html
    assert "We couldn't tell from your profile" in html
    assert escape(UNCLEAR_REASON) in html
    assert 'href="/profile"' in html
    assert "What to do" not in html
    assert "Why this" not in html
    assert "Obligations in the text" in html
    assert "is%20marked%20unclear" in html


def test_detail_applies_zero_todos_and_does_not(pages_client: TestClient) -> None:
    html = pages_client.get(f"/change/{_change_id(GII_URL)}").text
    assert "Why this applies to you" in html
    assert "What to do" in html
    assert "No to-dos yet — the obligations below are the source text." in html
    assert html.count("<blockquote>") == 1
    assert 'href="https://www.gesetze-im-internet.de/bgb/__356a.html"' in html
    assert "nvidia/Nemotron-3_5-Lightning" in html  # extracted by a model, not the seed run

    html = pages_client.get(f"/change/{_change_id(SHOPIFY_URL)}").text
    assert "Doesn't apply to you" in html
    assert "Why this doesn't apply to you" in html
    assert "What to do" not in html
    assert "No to-dos yet" not in html
    assert escape("This page covers US returns; you sell to EU consumers only.") in html
    assert "is%20marked%20doesn%27t%20apply" in html


def test_detail_source_metadata_and_mailto(seed_client: TestClient) -> None:
    import regwatch.app as appmod
    from regwatch.app import wrong_call_mailto

    change_id = seed_change_id(ART_11A)
    payload = seed_payload(seed_file(ART_11A))
    with session(appmod.engine) as s:
        ch = s.get(RegChangeRow, change_id)
        assert ch is not None
        doc = s.get(DocumentRow, ch.document_url)
        assert doc is not None
        content_hash = doc.content_hash
        published = doc.published_date
    html = seed_client.get(f"/change/{change_id}").text
    assert "<dt>Extracted by</dt><dd>recorded seed run</dd>" in html
    assert f"{payload['model']} · ${payload['cost_usd']:.4f}" in html
    assert f"<code>{content_hash[:12]}</code>" in html
    assert published is None  # Tavily returned no date for the ELI page: no row, none invented
    assert "<dt>Published</dt>" not in html
    assert "<dt>Fetched</dt><dd>" in html and " UTC</dd>" in html
    assert f"<dt>Jurisdiction</dt><dd>{escape(payload['change']['jurisdiction'])}</dd>" in html
    mailto = wrong_call_mailto(change_id, "applies")
    assert mailto == (
        "mailto:?subject=RegWatch%20wrong%20call%3A%20change%20%23"
        f"{change_id}%20is%20marked%20applies"
    )
    assert f'<a class="wrong" href="{mailto}">Report a wrong call</a>' in html


def test_detail_mailto_uses_report_email_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REGWATCH_DB_URL", f"sqlite:///{tmp_path / 'mail.db'}")
    monkeypatch.setenv("REGWATCH_REPORT_EMAIL", "ops@example.org")
    import regwatch.app as appmod

    appmod = importlib.reload(appmod)
    from regwatch.jobs.crawl import seed

    seed()
    html = TestClient(appmod.app).get("/change/1").text
    assert 'href="mailto:ops@example.org?subject=RegWatch%20wrong%20call' in html


def test_detail_unchecked_change_renders(pages_client: TestClient) -> None:
    bmj_id = _change_id(BMJ_URL)
    assert bmj_id == SEED_CHANGES + 2
    r = pages_client.get(f"/change/{bmj_id}")
    assert r.status_code == 200
    assert "Not checked against your profile yet" in r.text
    assert "regwatch crawl --stage classify" in r.text
    assert 'class="pill' not in r.text
    assert "Report a wrong call" not in r.text
    assert "Obligations in the text" in r.text
    assert 'class="chip">bmj.de' in r.text
    assert "<dt>Classified by</dt>" not in r.text
    assert pages_client.get("/change/999").status_code == 404


def test_detail_redirects_to_profile_on_empty_db(empty_client: TestClient) -> None:
    r = empty_client.get("/change/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/profile"


def test_detail_unchecked_change_without_document_row(pages_client: TestClient) -> None:
    """A change whose source document is gone still renders, with the URL host as its source."""
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        row = RegChangeRow(
            document_url="https://commission.europa.eu/some-page",
            instrument="Commission notice",
            jurisdiction="EU",
            summary="s",
            effective_dates="[]",
            obligations="[]",
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.id is not None
        new_id = row.id
    html = pages_client.get(f"/change/{new_id}").text
    assert 'class="chip">commission.europa.eu' in html
    assert "<dt>Content hash</dt>" not in html
    feed = pages_client.get("/feed").text
    assert "Commission notice" in _section(feed, "unchecked")


# ---------- Oct 22 (R4): two subline fixes from the Oct 21 verifier ----------


def test_feed_subline_shows_category_plain_names(seed_client: TestClient) -> None:
    html = seed_client.get("/feed").text
    assert "Furniture and wooden goods, Leather goods" in html
    assert "furniture_wood" not in html


def test_feed_single_item_group_omits_instrument_subline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # one recorded seed change per regulation → each group has one item and no law subline
    one_each = tmp_path / "one-each"
    one_each.mkdir()
    for name in ("profile.json", "documents.jsonl"):
        shutil.copy(SEED_DIR / name, one_each / name)
    eudr = next(p for p in SEED_FILES if p.stem.endswith("-eudr"))
    for path in (seed_file(ART_11A), eudr):
        shutil.copy(path, one_each / path.name)
    monkeypatch.setenv("REGWATCH_SEED_DIR", str(one_each))
    appmod = _reload_app(tmp_path, monkeypatch, "one-each.db")
    from regwatch.jobs.crawl import seed

    assert seed() == 2
    html = TestClient(appmod.app).get("/feed").text
    assert '<p class="law">' not in html
    for r in SEED_REGULATIONS:
        assert escape(r["instrument"]) not in html
    assert html.count("<h3>") == 2


def test_feed_seed_groups_keep_instrument_subline(seed_client: TestClient) -> None:
    html = seed_client.get("/feed").text
    for r in SEED_REGULATIONS:
        assert html.count(f'<p class="law">{escape(r["instrument"])}</p>') == 1


def test_feed_multi_item_group_keeps_instrument_subline(pages_client: TestClient) -> None:
    html = pages_client.get("/feed").text
    assert _section(html, "withdrawal").count('<p class="law">') == 1
    assert _section(html, "eudr").count('<p class="law">') == 1


# ---------- sectioned acts collapse their non-applying sections ----------

ACT_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32023L2673"
ACT_NAME = "Directive (EU) 2019/2161"  # not an act of the recorded seed


def _collapse_lines(html: str) -> list[str]:
    return [c.split("</p>")[0] for c in html.split('<p class="muted collapsed">')[1:]]


def _act_collapse_lines(html: str) -> list[str]:
    return [line for line in _collapse_lines(html) if ACT_NAME in line]


def _add_sections(
    client: TestClient,
    url: str,
    decisions: list[str],
    tag: str = "withdrawal",
    *,
    content_hash: str = "hash-act",
    first: int = 1,
    day: int = 10,
) -> None:
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        prof = appmod.current_profile(s)
        assert prof is not None and prof.id is not None
        doc = s.get(DocumentRow, url) or DocumentRow(
            url=url,
            content_hash=content_hash,
            source_domain="eur-lex.europa.eu",
            published_date="2023-11-28",
            fetched_at=datetime(2026, 7, 1, 8, 0),
            markdown="# Act\n\nQuote.\n",
            regulation_tags=json.dumps([tag]),
        )
        doc.content_hash = content_hash
        s.add(doc)
        for n, decision in enumerate(decisions, start=first):
            row = RegChangeRow(
                document_url=url,
                content_hash=content_hash,
                model="nvidia/Nemotron-3_5-Lightning",
                instrument=f"{ACT_NAME} — Article {n}",
                jurisdiction="EU",
                summary=f"Section {n} summary.",
                effective_dates="[]",
                obligations=json.dumps([{"text": "Do it.", "quote": "Quote."}]),
                created_at=datetime(2026, 7, day, 9, n),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            assert row.id is not None
            s.add(
                ApplicabilityRow(
                    profile_id=prof.id,
                    regchange_id=row.id,
                    decision=decision,
                    reasons=json.dumps(["Because."]),
                    todos="[]",
                    model="nvidia/Nemotron-3-Ultra-550b-a55b",
                    cost_usd=0.01,
                )
            )
            s.commit()


def test_feed_collapses_does_not_sections_under_act(seed_client: TestClient) -> None:
    _add_sections(seed_client, ACT_URL, ["applies", "does_not", "does_not"])
    html = seed_client.get("/feed").text
    assert f"2 other provisions of {ACT_NAME} don&#39;t apply to you" in html
    assert f"{ACT_NAME} — Article 1" in html
    assert f"{ACT_NAME} — Article 2" not in html
    assert f"{ACT_NAME} — Article 3" not in html
    assert len(_act_collapse_lines(html)) == 1


def test_feed_collapse_line_singular(seed_client: TestClient) -> None:
    _add_sections(seed_client, ACT_URL, ["applies", "unclear", "does_not"])
    html = seed_client.get("/feed").text
    assert f"1 other provision of {ACT_NAME} doesn&#39;t apply to you" in html
    assert f"{ACT_NAME} — Article 1" in html and f"{ACT_NAME} — Article 2" in html
    assert f"{ACT_NAME} — Article 3" not in html


def test_feed_all_does_not_act_is_one_item_with_count(seed_client: TestClient) -> None:
    _add_sections(seed_client, ACT_URL, ["does_not", "does_not", "does_not"])
    html = seed_client.get("/feed").text
    assert html.count(f"{ACT_NAME} — Article") == 1
    assert f"2 other provisions of {ACT_NAME} don&#39;t apply to you" in html


def test_feed_old_version_does_not_is_not_counted_against_new_version(
    seed_client: TestClient,
) -> None:
    _add_sections(seed_client, ACT_URL, ["does_not"], content_hash="hash-old", day=1)
    _add_sections(seed_client, ACT_URL, ["applies"], content_hash="hash-new", first=2, day=10)
    html = seed_client.get("/feed").text
    assert f"{ACT_NAME} — Article 2" in html
    assert _act_collapse_lines(html) == []


def test_feed_collapse_counts_only_the_newest_versions_sections(seed_client: TestClient) -> None:
    _add_sections(seed_client, ACT_URL, ["does_not"], content_hash="hash-old", day=1)
    _add_sections(
        seed_client,
        ACT_URL,
        ["applies", "does_not", "does_not"],
        content_hash="hash-new",
        first=2,
        day=10,
    )
    html = seed_client.get("/feed").text
    assert f"2 other provisions of {ACT_NAME} don&#39;t apply to you" in html


def _n_seed_does_not(act: str) -> int:
    return sum(
        seed_payload(p)["change"]["instrument"].startswith(f"{act} — ")
        and seed_payload(p)["applicability"]["decision"] == "does_not"
        for p in SEED_FILES
    )


def test_feed_seed_collapses_non_applying_sections(seed_client: TestClient) -> None:
    html = seed_client.get("/feed").text
    n_2673 = _n_seed_does_not("Directive (EU) 2023/2673")
    n_1115 = _n_seed_does_not("Regulation (EU) 2023/1115")
    assert n_2673 >= 2 and n_1115 >= 2
    assert _collapse_lines(html) == [
        f"{n_2673} other provisions of Directive (EU) 2023/2673 don&#39;t apply to you",
        f"{n_1115} other provisions of Regulation (EU) 2023/1115 don&#39;t apply to you",
    ]
    assert _collapse_lines(html)[0] in _section(html, "withdrawal")
    assert "Directive (EU) 2023/2673 — Article 1, point (1)" not in html  # a does_not section
    assert escape(ART_11A) in _section(html, "withdrawal")


def test_feed_single_change_documents_have_no_collapse_line(pages_client: TestClient) -> None:
    html = pages_client.get("/feed").text
    lines = _collapse_lines(html)
    assert len(lines) == 2  # only the recorded seed's two sectioned acts
    for spec in EXTRA_ROWS:
        assert not any(spec["instrument"] in line for line in lines)


def test_month_and_detail_render_sectioned_instrument(seed_client: TestClient) -> None:
    _add_sections(seed_client, ACT_URL, ["applies", "does_not"])
    import regwatch.app as appmod

    with session(appmod.engine) as s:
        rows = s.exec(select(RegChangeRow).where(RegChangeRow.document_url == ACT_URL)).all()
    assert seed_client.get("/month").status_code == 200
    detail = seed_client.get(f"/change/{rows[0].id}")
    assert detail.status_code == 200
    assert f"{ACT_NAME} — Article 1" in detail.text


@pytest.mark.parametrize("tag", ["", "not-a-seed-tag"])
def test_feed_collapses_sections_of_untagged_act(seed_client: TestClient, tag: str) -> None:
    _add_sections(seed_client, ACT_URL, ["applies", "does_not", "does_not"], tag=tag)
    html = seed_client.get("/feed").text
    assert html.count(f"2 other provisions of {ACT_NAME} don&#39;t apply to you") == 1
    assert f"{ACT_NAME} — Article 1" in html
    assert f"{ACT_NAME} — Article 2" not in html
    assert f"{ACT_NAME} — Article 3" not in html
    assert f"<h2>{ACT_NAME}</h2>" in html
