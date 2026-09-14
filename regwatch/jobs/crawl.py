"""The scheduled job (Nebius Serverless Job): search → extract → diff → normalize → classify → store.

Today only `seed` works: it loads examples/seed-run/*.json so the demo URL is never empty.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import col, select

from regwatch.config import Settings
from regwatch.db import ApplicabilityRow, ProfileRow, RegChangeRow, make_engine, session
from regwatch.schemas import Applicability, Profile, RegChange

SEED_DIR = Path(__file__).resolve().parents[2] / "examples" / "seed-run"


def seed(settings: Settings | None = None) -> int:
    """Load the recorded seed run. Idempotent: skips changes whose document_url already exists."""
    settings = settings or Settings()
    engine = make_engine(settings)
    profile = Profile.model_validate_json((SEED_DIR / "profile.json").read_text(encoding="utf-8"))
    added = 0
    with session(engine) as s:
        prof = s.exec(select(ProfileRow).order_by(col(ProfileRow.id).desc())).first()
        if prof is None:
            prof = ProfileRow(
                establishment_country=profile.establishment_country,
                ship_to=json.dumps(profile.ship_to),
                categories=json.dumps(profile.categories),
                platform=profile.platform,
            )
            s.add(prof)
            s.commit()
            s.refresh(prof)
        assert prof.id is not None
        for path in sorted(SEED_DIR.glob("change-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            change = RegChange.model_validate(payload["change"])
            appl = Applicability.model_validate(payload["applicability"])
            if s.exec(
                select(RegChangeRow).where(RegChangeRow.document_url == change.document_url)
            ).first():
                continue
            if appl.ungrounded_todos(change):
                raise ValueError(f"{path.name}: to-do quote not grounded in obligations")
            row = RegChangeRow(
                document_url=change.document_url,
                instrument=change.instrument,
                jurisdiction=change.jurisdiction,
                summary=change.summary,
                effective_dates=json.dumps([d.isoformat() for d in change.effective_dates]),
                obligations=json.dumps([o.model_dump() for o in change.obligations]),
                created_at=datetime.fromisoformat(
                    payload.get("created_at", datetime.now(UTC).isoformat())
                ),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            assert row.id is not None
            s.add(
                ApplicabilityRow(
                    profile_id=prof.id,
                    regchange_id=row.id,
                    decision=appl.decision,
                    reasons=json.dumps(appl.reasons),
                    todos=json.dumps([t.model_dump(mode="json") for t in appl.todos]),
                    model=payload.get("model", settings.classifier_model),
                    cost_usd=float(payload.get("cost_usd", 0.0)),
                    tavily_credits=int(payload.get("tavily_credits", 0)),
                )
            )
            s.commit()
            added += 1
    return added


def crawl(settings: Settings | None = None) -> int:
    raise NotImplementedError("Oct 18–20 — search → extract → diff → normalize → classify")
