"""Lightning: Document → RegChange with verbatim obligation quotes. Lands Oct 19.

Quotes are validated with RegChange.validate_quotes(); one retry with the failures listed, then
the document is logged as 'not normalised' and skipped (never fatal).
"""

from __future__ import annotations

from regwatch.schemas import RegChange


def normalize(document_url: str, markdown: str) -> RegChange | None:
    raise NotImplementedError("Oct 19 — see docs/regwatch-plan.md §4")
