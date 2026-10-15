import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from regwatch.config import CATEGORIES, EU_COUNTRIES, PLATFORMS, REFERENCE_TAG, SOURCE_DOMAINS
from regwatch.ingest.tavily_backend import allowed_domain
from regwatch.moves import read_moves
from regwatch.schemas import Applicability, Profile, RegChange

SEED_DIR = Path(__file__).resolve().parent.parent / "examples" / "seed-run"
CHANGE_FILES = sorted(SEED_DIR.glob("change-*.json"))


def test_seed_change_files_present() -> None:
    """Guard for the parametrised grounding test: it must never pass vacuously."""
    assert len(CHANGE_FILES) >= 2


def test_demo_profile_matches_config() -> None:
    profile = Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    assert profile.establishment_country in EU_COUNTRIES
    assert set(profile.ship_to) <= set(EU_COUNTRIES)
    assert set(profile.categories) <= {k for k, _ in CATEGORIES}
    assert profile.platform in {k for k, _ in PLATFORMS}
    # EUDR dates and duties depend on these (Regulation (EU) 2025/2650)
    assert profile.company_size == "small"


@pytest.mark.parametrize("path", CHANGE_FILES, ids=[p.name for p in CHANGE_FILES])
def test_seed_changes_are_grounded(path: Path) -> None:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    change = RegChange.model_validate(payload["change"])
    appl = Applicability.model_validate(payload["applicability"])
    assert appl.ungrounded_todos(change) == []
    assert all(o.quote.strip() for o in change.obligations)
    assert all(t.citation_url == change.document_url for t in appl.todos)
    # the ingest allow-list rule (exact host or subdomain, e.g. www.ihk.de → ihk.de)
    assert allowed_domain(change.document_url) in SOURCE_DOMAINS
    assert urlparse(change.document_url).scheme == "https"
    assert all(t.due is not None for t in appl.todos)


def test_seed_run_documents_are_seed_change_sources() -> None:
    """R1: every recorded document is the real source of a seed change (never a fixture body), or
    a REFERENCE_TAG document that grounds a rule move (e.g. Regulation (EU) No 995/2010's Annex)."""
    assert len(CHANGE_FILES) >= 2
    sources = {
        json.loads(p.read_text(encoding="utf-8"))["change"]["document_url"] for p in CHANGE_FILES
    }
    lines = (SEED_DIR / "documents.jsonl").read_text(encoding="utf-8").splitlines()
    docs = [json.loads(line) for line in lines if line.strip()]
    urls = [d["url"] for d in docs]
    assert urls and urls == sorted(urls)
    refs = {d["url"] for d in docs if REFERENCE_TAG in d["regulation_tags"]}
    assert set(urls) == sources | refs and not sources & refs
    grounding = {u for m in read_moves(SEED_DIR / "rule-moves.json") for u in m.source_urls}
    assert refs and refs <= grounding  # no reference document ships without a move using it


EUDR_FILES = sorted(SEED_DIR.glob("change-*-eudr.json"))
WOOD_DUE, LEATHER_DUE = "2026-12-30", "2027-06-30"


def test_default_store_eudr_wood_keeps_30_december_2026_leather_moves() -> None:
    """Art. 38(3) EUDR (as amended by 2025/2650) moves small operators to 30 June 2027 "except
    as regards the products covered by the Annex to Regulation (EU) No 995/2010", which lists
    wooden furniture: every EUDR to-do of the default store is split into a wooden-furniture
    to-do due 30 December 2026 and a leather-goods to-do due 30 June 2027, same obligation quote."""
    n_split = 0
    for path in EUDR_FILES:
        appl = json.loads(path.read_text(encoding="utf-8"))["applicability"]
        todos = appl["todos"]
        by_quote = Counter(t["quote"] for t in todos)
        for t in todos:
            assert by_quote[t["quote"]] == 2, (path.name, t["text"])
            if t["due"] == WOOD_DUE:
                assert "wooden furniture" in t["text"] and "leather" not in t["text"]
            else:
                assert t["due"] == LEATHER_DUE and "leather" in t["text"]
                assert "wood" not in t["text"] and "furniture" not in t["text"]
        dues = Counter((t["quote"], t["due"]) for t in todos)
        assert all(n == 1 for n in dues.values())
        n_split += len(by_quote)
        for reason in appl["reasons"]:
            if "2027" in reason:
                assert "30 December 2026" in reason and "wooden furniture" in reason, reason
    assert n_split == 14


def test_seed_refuses_a_split_todo_with_an_ungrounded_quote(tmp_path: Path) -> None:
    from regwatch.config import Settings
    from regwatch.jobs.crawl import seed

    seed_dir = tmp_path / "seed-run"
    shutil.copytree(SEED_DIR, seed_dir)
    path = seed_dir / EUDR_FILES[0].name
    payload = json.loads(path.read_text(encoding="utf-8"))
    wood = payload["applicability"]["todos"][0]
    wood["quote"] = wood["quote"] + " (wooden furniture only)"  # not an obligation quote
    path.write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(
        REGWATCH_DB_URL=f"sqlite:///{tmp_path / 's.db'}", REGWATCH_SEED_DIR=str(seed_dir)
    )
    with pytest.raises(ValueError, match="to-do quote not grounded in obligations"):
        seed(settings)


def test_seed_twice_loads_split_todos_once_and_skips_reference_docs(tmp_path: Path) -> None:
    from sqlmodel import Session, select

    from regwatch.config import Settings
    from regwatch.db import (
        ApplicabilityRow,
        DocumentRow,
        documents_needing_normalize,
        make_engine,
    )
    from regwatch.jobs.crawl import seed

    settings = Settings(REGWATCH_DB_URL=f"sqlite:///{tmp_path / 's.db'}")
    assert seed(settings) == len(CHANGE_FILES)
    with Session(make_engine(settings)) as s:
        before = sorted(
            (a.regchange_id, a.profile_id, a.todos) for a in s.exec(select(ApplicabilityRow))
        )
    assert seed(settings) == 0
    with Session(make_engine(settings)) as s:
        after = sorted(
            (a.regchange_id, a.profile_id, a.todos) for a in s.exec(select(ApplicabilityRow))
        )
        refs = [d for d in s.exec(select(DocumentRow)) if REFERENCE_TAG in d.regulation_tags]
        # reference documents ground rule moves; normalize must never turn them into changes
        assert len(refs) == 3 and documents_needing_normalize(s) == []
    assert before == after
    dues = Counter(
        t["due"]
        for _, _, todos in after
        for t in json.loads(todos)
        if "wooden furniture" in t["text"]
    )
    assert dues == {WOOD_DUE: 14}


def _all_seed_appls() -> list[tuple[str, dict[str, Any]]]:
    out = [
        (p.name, json.loads(p.read_text(encoding="utf-8"))["applicability"]) for p in CHANGE_FILES
    ]
    for p in sorted((SEED_DIR / "stores").glob("*.applicability.jsonl")):
        lines = p.read_text(encoding="utf-8").splitlines()
        out += [(p.name, json.loads(line)["applicability"]) for line in lines if line.strip()]
    return out


def test_every_todo_reads_on_its_own() -> None:
    """The month view sorts by date, so a leather-goods to-do lands far from its wood twin: no
    to-do may lean on another ("Extend that procedure to your leather goods")."""
    leaning = [
        (name, t["text"])
        for name, appl in _all_seed_appls()
        for t in appl["todos"]
        if t["text"].lower().startswith(("extend that", "add your leather goods to that"))
        or " that procedure" in t["text"].lower()
    ]
    assert leaning == []


def test_no_todo_is_repeated_within_a_store() -> None:
    """One store sees each to-do text once (the withdrawal sources used to repeat each other)."""
    for name in {n for n, _ in _all_seed_appls()}:
        texts = [
            t["text"].strip().lower()
            for n, appl in _all_seed_appls()
            if n == name
            for t in appl["todos"]
        ]
        assert len(texts) == len(set(texts)), name
