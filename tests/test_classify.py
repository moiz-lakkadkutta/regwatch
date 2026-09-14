import json
import logging
import re
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlmodel import Session, select

from regwatch.config import EU_COUNTRIES, Settings
from regwatch.db import (
    ApplicabilityRow,
    ProfileRow,
    RegChangeRow,
    make_engine,
    regchanges_needing_classify,
    store_regchange,
)
from regwatch.jobs.crawl import SEED_DIR
from regwatch.schemas import Applicability, Document, Obligation, Profile, RegChange, Todo
from regwatch.understand.classify import (
    MAX_TODOS,
    ClassifiedPayload,
    ClassifyResult,
    TodoDraft,
    build_messages,
    classify,
    classify_all,
    enforce,
    load_prompt,
    parse_payload,
    render_profile,
    response_format,
    response_json_schema,
    retry_message,
)
from regwatch.understand.llm_backend import (
    FixtureLLMBackend,
    LLMResult,
    LLMUsage,
    ModelLedger,
)
from regwatch.understand.normalize import normalize
from regwatch.understand.normalize import parse_payload as parse_normalized
from tests.conftest import FIXTURES_ROOT, LLM_FIXTURES_ROOT

EXTRACT = FIXTURES_ROOT / "extract"
MANIFEST: list[dict[str, Any]] = json.loads(
    (FIXTURES_ROOT / "manifest.json").read_text(encoding="utf-8")
)
CORE = [m for m in MANIFEST if m["kind"] == "core"]
BY_ID = {m["id"]: m for m in MANIFEST}
URL_2673 = BY_ID["eurlex-2023-2673"]["url"]
URL_GII = BY_ID["gii-356a-bgb"]["url"]
URL_1115 = BY_ID["eurlex-2023-1115"]["url"]
SHORTENED_GII_QUOTE = (
    'Die Widerrufsfunktion ist mit den Worten „Vertrag hier widerrufen" zu kennzeichnen.'
)


def _profile() -> Profile:
    return Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))


def _change(doc_id: str) -> RegChange:
    """The RegChange Oct 19's normalize() produces for this fixture document."""
    m = BY_ID[doc_id]
    doc = Document(
        url=m["url"],
        content_hash="h-" + doc_id,
        fetched_at=datetime.now(UTC),
        markdown=(EXTRACT / f"{doc_id}.md").read_text(encoding="utf-8"),
        source_domain=m["source_domain"],
    )
    settings = Settings()
    result = normalize(
        doc,
        backend=FixtureLLMBackend(LLM_FIXTURES_ROOT / "normalize"),
        settings=settings,
        ledger=ModelLedger.for_extractor(settings),
    )
    if result.change is not None:
        return result.change
    # shopify-help-us-returns fails normalisation in the Oct 19 fixtures: stub it from the manifest
    assert doc_id == "shopify-help-us-returns"
    return RegChange(
        document_url=m["url"],
        instrument="FTC Mail, Internet, or Telephone Order Merchandise Rule — Shopify Help Center",
        jurisdiction="US",
        summary=m["why"],
        effective_dates=[],
        obligations=[
            Obligation(
                text="Display your return policy at checkout for US customers.",
                quote=(
                    "Merchants must display their return policy at checkout for all customers "
                    "in the United States."
                ),
            )
        ],
    )


def _backend() -> FixtureLLMBackend:
    return FixtureLLMBackend(LLM_FIXTURES_ROOT / "classify")


def _run(doc_id: str, backend: FixtureLLMBackend | None = None) -> ClassifyResult:
    settings = Settings()
    return classify(
        _change(doc_id),
        _profile(),
        backend=backend or _backend(),
        settings=settings,
        ledger=ModelLedger.for_classifier(settings),
    )


class StubBackend:
    """Replays the same replies for any Source URL; records every call."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def complete(
        self, *, model: str, messages: list[dict[str, str]], response_format: dict[str, Any] | None
    ) -> LLMResult:
        self.calls.append([dict(m) for m in messages])
        return LLMResult(
            text=self.replies.pop(0),
            usage=LLMUsage(prompt_tokens=100, completion_tokens=20),
            model=model,
        )


def _payload(decision: str, todos: list[dict[str, Any]], reasons: list[str] | None = None) -> str:
    return json.dumps(
        {"decision": decision, "reasons": reasons or ["You sell furniture."], "todos": todos}
    )


# --- labels and grounding on the core fixtures --------------------------------------------------


@pytest.mark.parametrize("doc", CORE, ids=[m["id"] for m in CORE])
def test_core_fixtures_match_expected_labels(doc: dict[str, Any]) -> None:
    result = _run(doc["id"])
    assert result.error is None and result.applicability is not None
    assert result.applicability.decision == doc["expected_decision"]


def test_core_label_counts_are_3_2_1() -> None:
    applicabilities = [_run(m["id"]).applicability for m in CORE]
    decisions = [a.decision for a in applicabilities if a is not None]
    assert len(decisions) == len(CORE)
    assert (
        decisions.count("applies"),
        decisions.count("does_not"),
        decisions.count("unclear"),
    ) == (3, 2, 1)
    assert len(CORE) == 6


def test_todos_grounded_and_citation_is_ours() -> None:
    n_todos = 0
    for m in CORE:
        change = _change(m["id"])
        result = _run(m["id"])
        appl = result.applicability
        assert appl is not None
        assert appl.ungrounded_todos(change) == []
        quotes = {" ".join(o.quote.split()) for o in change.obligations}
        for t in appl.todos:
            assert " ".join(t.quote.split()) in quotes
            assert t.citation_url == change.document_url
            n_todos += 1
    assert n_todos == 7


def test_ungrounded_todo_triggers_exactly_one_retry() -> None:
    backend = _backend()
    result = _run("gii-356a-bgb", backend)
    assert result.retried is True and result.dropped_todos == [] and result.error is None
    assert result.applicability is not None and len(result.applicability.todos) == 2
    assert len(backend.calls) == 2
    retry = backend.calls[1]
    assert [m["role"] for m in retry] == ["system", "user", "assistant", "user"]
    addendum = retry[-1]["content"]
    assert "1 to-do(s) whose quote was not copied exactly" in addendum
    assert SHORTENED_GII_QUOTE in addendum
    assert addendum.endswith(f"Source URL: {URL_GII}")
    assert result.usage is not None and result.usage.prompt_tokens == 1900 + 2400
    assert result.cost_usd == pytest.approx(
        (
            4300 * Settings().classifier_price_in_per_mtok
            + 830 * Settings().classifier_price_out_per_mtok
        )
        / 1e6
    )


def test_invented_due_is_nulled_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="regwatch.understand.classify"):
        result = _run("eurlex-2023-1115")
    appl = result.applicability
    assert appl is not None and appl.decision == "applies"
    assert appl.todos[0].due is None
    assert appl.todos[1].due == date(2026, 6, 30)
    assert result.nulled_dues == 1 and result.retried is False
    nulled = [r for r in caplog.records if "nulled invented due" in r.message]
    assert len(nulled) == 1 and "2026-01-01" in nulled[0].message


def test_applies_with_all_todos_dropped_keeps_decision(caplog: pytest.LogCaptureFixture) -> None:
    change = _change("eurlex-2023-2673")
    bad = _payload("applies", [{"text": "Add the button", "due": None, "quote": "not a quote"}])
    backend = StubBackend([bad, bad])
    settings = Settings()
    with caplog.at_level(logging.WARNING, logger="regwatch.understand.classify"):
        result = classify(
            change,
            _profile(),
            backend=backend,
            settings=settings,
            ledger=ModelLedger.for_classifier(settings),
        )
    assert result.applicability is not None
    assert result.applicability.decision == "applies" and result.applicability.todos == []
    assert result.retried is True and len(result.dropped_todos) == 1
    assert result.dropped_todos[0].quote == "not a quote"
    assert len(backend.calls) == 2
    assert any("applies with 0 grounded to-dos" in r.message for r in caplog.records)
    assert any("dropped ungrounded to-do" in r.message for r in caplog.records)


def test_does_not_todos_are_stripped_and_unclear_keeps_at_most_one() -> None:
    change = _change("eurlex-2023-2673")
    q = [o.quote for o in change.obligations]
    grounded = [
        {"text": "Add the button", "due": "2026-06-19", "quote": q[0]},
        {"text": "Label it", "due": "2026-06-19", "quote": q[1]},
    ]
    settings = Settings()
    ledger = ModelLedger.for_classifier(settings)
    does_not = classify(
        change,
        _profile(),
        backend=StubBackend([_payload("does_not", grounded)]),
        settings=settings,
        ledger=ledger,
    )
    assert does_not.applicability is not None
    assert does_not.applicability.decision == "does_not"
    assert does_not.applicability.todos == [] and does_not.dropped_todos == []
    assert does_not.retried is False
    unclear = classify(
        change,
        _profile(),
        backend=StubBackend([_payload("unclear", grounded)]),
        settings=settings,
        ledger=ledger,
    )
    assert unclear.applicability is not None and unclear.applicability.decision == "unclear"
    assert [t.quote for t in unclear.applicability.todos] == [q[0]]
    assert unclear.retried is False
    # enforce() alone: MAX_TODOS cap and the unclear rule drop ungrounded ones without a retry
    many = ClassifiedPayload(
        decision="applies",
        reasons=["r"],
        todos=[
            TodoDraft(text=f"t{i}", due=None, quote=q[i % len(q)]) for i in range(MAX_TODOS + 2)
        ],
    )
    appl, rejected, nulled = enforce(many, change)
    assert len(appl.todos) == MAX_TODOS and rejected == [] and nulled == 0


# --- copy, prompt and schema ---------------------------------------------------------------------


def test_reasons_use_profile_terms() -> None:
    terms = ("Germany", "furniture", "leather", "EU countries")
    for m in CORE:
        appl = _run(m["id"]).applicability
        assert appl is not None and appl.reasons
        assert any(t in " ".join(appl.reasons) for t in terms), m["id"]
        for reason in appl.reasons:
            assert not re.search(r"Art\.|Article \d|recital", reason), reason
        if appl.decision == "unclear":
            assert appl.reasons[0].startswith("We couldn't tell from your profile:")
        for t in appl.todos:
            assert t.text[0].isupper() and " " in t.text  # verb phrase with an object


def test_prompt_renders_profile_and_change() -> None:
    change = _change("eurlex-2023-2673")
    messages = build_messages(change, _profile())
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert '"additionalProperties": false' in system
    assert "## JSON schema" in system and "We couldn't tell from your profile:" in system
    assert "- Established in: Germany (DE)" in user
    assert "- Ships to: Austria (AT), Belgium (BE), Germany (DE)," in user
    assert "- Sells: Furniture and wooden goods (furniture_wood); Leather goods (leather)" in user
    assert "- Platform: Shopify" in user
    assert "- Effective dates: 2026-06-19" in user
    assert "  1. Provide a withdrawal function on the online interface." in user
    assert f'     quote: "{change.obligations[0].quote}"' in user
    assert "  4. " in user
    assert user.endswith(f"Source URL: {URL_2673}")
    everywhere = render_profile(
        Profile(establishment_country="FR", ship_to=list(EU_COUNTRIES), categories=["other"])
    )
    assert "- Ships to: all 27 EU countries" in everywhere and "France (FR)" in everywhere
    assert load_prompt() is load_prompt()
    addendum = retry_message(change, [TodoDraft(text="Do x", due=None, quote="bad")])
    assert addendum.startswith("Your previous answer had 1 to-do(s)")
    assert '- "Do x" — quote given: "bad"' in addendum
    assert addendum.endswith(f"Source URL: {URL_2673}")
    assert retry_message(change, []).startswith("Your previous answer was not one valid JSON")


def test_prompt_unclear_rule_matches_enforce() -> None:
    """The prompt says what enforce() does for "unclear": at most one grounded to-do."""
    assert "at most one\n  to-do that asks the one question that would decide it" in load_prompt()
    assert '"unclear" → an empty list.' not in load_prompt()


def test_response_format_is_strict() -> None:
    fmt = response_format()
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "classified_payload"
    schema = response_json_schema()
    assert fmt["json_schema"]["schema"] == schema
    dumped = json.dumps(schema)
    for key in ("title", "default", "format", "minItems", "maxItems"):
        assert f'"{key}"' not in dumped
    objects = [schema, *schema["$defs"].values()]
    assert len(objects) == 2
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert set(schema["properties"]["decision"]["enum"]) == {"applies", "does_not", "unclear"}
    assert "citation_url" not in schema["$defs"]["TodoDraft"]["properties"]
    assert schema["$defs"]["TodoDraft"]["properties"]["due"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }


def test_parse_payload_tolerates_fences_and_prose() -> None:
    raw = _payload("does_not", [])
    for text in (
        raw,
        f"Here you go:\n```json\n{raw}\n```",
        f"<think>weighing {{ scope }}</think>{raw}",
        f"Sure! {raw} Hope this helps.",
    ):
        assert parse_payload(text) == ClassifiedPayload.model_validate_json(raw)
    for bad, kind in (("no json", "invalid_json"), ('{"decision": "maybe"}', "schema")):
        with pytest.raises(Exception) as exc:
            parse_payload(bad)
        assert getattr(exc.value, "kind", None) == kind


# --- failures and persistence --------------------------------------------------------------------


def test_unparseable_twice_is_failed_not_stored(tmp_settings: Settings) -> None:
    change = _change("eurlex-2023-2673")
    backend = StubBackend(["I cannot decide this.", "Still prose, sorry."])
    result = classify(
        change,
        _profile(),
        backend=backend,
        settings=tmp_settings,
        ledger=ModelLedger.for_classifier(tmp_settings),
    )
    assert result.applicability is None and result.error == "invalid_json"
    assert result.retried is True and len(backend.calls) == 2
    assert backend.calls[1][-1]["content"].startswith("Your previous answer was not one valid JSON")
    assert result.cost_usd > 0

    engine = make_engine(tmp_settings)
    with Session(engine) as s:
        prof = _profile_row(s)
        store_regchange(s, change, content_hash="h", model="test")
        summary = classify_all(
            s,
            profile_row=prof,
            backend=StubBackend(["nope", "nope"]),
            settings=tmp_settings,
            ledger=ModelLedger.for_classifier(tmp_settings),
        )
        assert summary.n_changes == 1 and summary.n_failed == 1
        assert summary.n_applies == summary.n_does_not == summary.n_unclear == 0
        assert s.exec(select(ApplicabilityRow)).all() == []
        assert len(regchanges_needing_classify(s, prof.id or 0)) == 1  # retried next crawl
        assert summary.line().startswith(
            "classify: 1 changes · 0 apply · 0 don't · 0 unclear · 0 retried · 1 failed · "
            "0 to-dos (0 dropped) · $"
        )


def test_classify_all_refuses_leaked_ungrounded_todo(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The store-boundary re-check in classify_all is its own barrier: even if enforce() leaked an
    ungrounded to-do, nothing is stored and the change counts as failed."""
    change = _change("eurlex-2023-2673")

    def leaky_enforce(
        payload: ClassifiedPayload, change: RegChange
    ) -> tuple[Applicability, list[Todo], int]:
        leaked = Todo(text="Do it", quote="NOT IN THE SOURCE", citation_url=change.document_url)
        return Applicability(decision="applies", reasons=["x"], todos=[leaked]), [], 0

    monkeypatch.setattr("regwatch.understand.classify.enforce", leaky_enforce)
    q = change.obligations[0].quote
    valid = _payload("applies", [{"text": "Add the button", "due": None, "quote": q}])
    backend = StubBackend([valid])
    engine = make_engine(tmp_settings)
    with Session(engine) as s:
        prof = _profile_row(s)
        store_regchange(s, change, content_hash="h", model="test")
        with caplog.at_level(logging.ERROR, logger="regwatch.understand.classify"):
            summary = classify_all(
                s,
                profile_row=prof,
                backend=backend,
                settings=tmp_settings,
                ledger=ModelLedger.for_classifier(tmp_settings),
            )
        assert len(backend.calls) == 1
        assert summary.n_changes == 1 and summary.n_failed == 1
        assert summary.n_applies == 0 and summary.n_todos == 0
        assert s.exec(select(ApplicabilityRow)).all() == []
        assert len(regchanges_needing_classify(s, prof.id or 0)) == 1
    assert "refusing to store ungrounded to-dos" in caplog.text


def _profile_row(s: Session, profile: Profile | None = None) -> ProfileRow:
    profile = profile or _profile()
    row = ProfileRow(
        establishment_country=profile.establishment_country,
        ship_to=json.dumps(profile.ship_to),
        categories=json.dumps(profile.categories),
        platform=profile.platform,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def _store_core_changes(s: Session) -> list[RegChange]:
    changes = [_change(m["id"]) for m in CORE]
    for c in changes:
        store_regchange(s, c, content_hash="h-" + c.document_url, model="test")
    return changes


def test_classify_all_is_idempotent_and_persists_cost(tmp_settings: Settings) -> None:
    engine = make_engine(tmp_settings)
    with Session(engine) as s:
        prof = _profile_row(s)
        changes = _store_core_changes(s)
        ledger = ModelLedger.for_classifier(tmp_settings)
        first = _backend()
        summary = classify_all(
            s, profile_row=prof, backend=first, settings=tmp_settings, ledger=ledger
        )
        assert summary.n_changes == len(changes) == 6
        assert (summary.n_applies, summary.n_does_not, summary.n_unclear) == (3, 2, 1)
        assert (summary.n_retried, summary.n_failed) == (1, 0)
        assert summary.n_todos == 7 and summary.n_todos_dropped == 0
        assert summary.cost_usd == pytest.approx(ledger.cost_usd) and summary.cost_usd > 0
        assert len(first.calls) == 7 and ledger.n_calls == 7
        assert summary.line().startswith(
            "classify: 6 changes · 3 apply · 2 don't · 1 unclear · 1 retried · 0 failed · "
            "7 to-dos (0 dropped) · $"
        )
        rows = s.exec(select(ApplicabilityRow)).all()
        assert len(rows) == 6
        assert all(r.model == tmp_settings.classifier_model for r in rows)
        assert all(r.cost_usd > 0 and r.tavily_credits == 0 for r in rows)
        assert sum(r.cost_usd for r in rows) == pytest.approx(summary.cost_usd)
        assert all(r.profile_id == prof.id for r in rows)
        for r in rows:
            appl = r.to_schema()
            change = next(c for c in changes if c.document_url == _url_of(s, r.regchange_id))
            assert appl.ungrounded_todos(change) == []

        second = _backend()
        again = classify_all(
            s, profile_row=prof, backend=second, settings=tmp_settings, ledger=ledger
        )
        assert again.n_changes == 0 and second.calls == []
        assert again.line().startswith("classify: 0 changes · 0 apply")
        assert len(s.exec(select(ApplicabilityRow)).all()) == 6


def _url_of(s: Session, regchange_id: int) -> str:
    row = s.get(RegChangeRow, regchange_id)
    assert row is not None
    return row.document_url


def test_new_profile_reclassifies_and_keeps_old_rows(tmp_settings: Settings) -> None:
    engine = make_engine(tmp_settings)
    with Session(engine) as s:
        prof = _profile_row(s)
        _store_core_changes(s)
        ledger = ModelLedger.for_classifier(tmp_settings)
        classify_all(s, profile_row=prof, backend=_backend(), settings=tmp_settings, ledger=ledger)
        old = {r.id: (r.profile_id, r.decision, r.todos) for r in s.exec(select(ApplicabilityRow))}
        assert len(old) == 6

        newer = _profile_row(
            s, Profile(establishment_country="FR", ship_to=["FR"], categories=["coffee_cocoa"])
        )
        assert len(regchanges_needing_classify(s, newer.id or 0)) == 6
        summary = classify_all(
            s, profile_row=newer, backend=_backend(), settings=tmp_settings, ledger=ledger
        )
        assert summary.n_changes == 6
        rows = s.exec(select(ApplicabilityRow)).all()
        assert len(rows) == 12
        assert {r.id: (r.profile_id, r.decision, r.todos) for r in rows if r.id in old} == old
        assert sum(r.profile_id == newer.id for r in rows) == 6
        assert regchanges_needing_classify(s, newer.id or 0) == []


# --- fixture hygiene -----------------------------------------------------------------------------


def test_classify_fixture_quotes_match_normalize_fixtures() -> None:
    classify_dir = LLM_FIXTURES_ROOT / "classify"
    files = sorted(classify_dir.glob("*.json"))
    assert len(files) == 16 and {p.stem for p in files} == set(BY_ID)
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["url"] == BY_ID[path.stem]["url"]
        assert data["responses"] and all(r["usage"]["prompt_tokens"] > 0 for r in data["responses"])
        last = parse_payload(data["responses"][-1]["text"])
        normalized = json.loads(
            (LLM_FIXTURES_ROOT / "normalize" / path.name).read_text(encoding="utf-8")
        )
        quotes = {
            " ".join(o.quote.split())
            for o in parse_normalized(normalized["responses"][-1]["text"]).obligations
        }
        for t in last.todos:
            assert " ".join(t.quote.split()) in quotes, (path.name, t.quote)
        m = BY_ID[path.stem]
        if m["kind"] == "core":
            assert last.decision == m["expected_decision"]
        elif path.stem == "shopify-help-eu-withdrawal":
            assert last.decision != "applies"  # R5: tag-merge doc is never an EUDR "applies"
        if last.decision != "applies":
            assert last.todos == []
