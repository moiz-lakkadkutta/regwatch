"""Ultra: (RegChange, Profile) → Applicability with grounded to-dos. Lands Oct 20.

Applicability.ungrounded_todos() must be empty; otherwise drop those to-dos and note it.
"""

from __future__ import annotations

from regwatch.schemas import Applicability, Profile, RegChange


def classify(change: RegChange, profile: Profile) -> Applicability:
    raise NotImplementedError("Oct 20 — see docs/regwatch-plan.md §4")
