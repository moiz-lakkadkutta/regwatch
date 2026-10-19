"""New vs changed vs unchanged, by content hash."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlmodel import Session

from regwatch.config import REFERENCE_TAG
from regwatch.db import DocumentRow
from regwatch.schemas import Document

log = logging.getLogger(__name__)


def content_hash(markdown: str) -> str:
    return hashlib.sha256(" ".join(markdown.split()).encode()).hexdigest()


@dataclass
class DiffResult:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.new) + len(self.changed) + len(self.unchanged)


def upsert_documents(s: Session, docs: Sequence[Document]) -> DiffResult:
    """Insert new rows, overwrite changed ones (by hash), leave unchanged rows untouched.

    One commit at the end.
    """
    result = DiffResult()
    for doc in docs:
        row = s.get(DocumentRow, doc.url)
        if row is None:
            s.add(
                DocumentRow(
                    url=doc.url,
                    content_hash=doc.content_hash,
                    source_domain=doc.source_domain,
                    published_date=doc.published_date,
                    fetched_at=doc.fetched_at,
                    markdown=doc.markdown,
                    regulation_tags=json.dumps(doc.regulation_tags),
                )
            )
            result.new.append(doc.url)
        elif row.content_hash != doc.content_hash:
            row.markdown = doc.markdown
            row.content_hash = doc.content_hash
            row.fetched_at = doc.fetched_at
            row.published_date = doc.published_date
            tags = list(doc.regulation_tags)
            if REFERENCE_TAG in json.loads(row.regulation_tags) and REFERENCE_TAG not in tags:
                tags.append(REFERENCE_TAG)  # a reference act stays out of normalise
            row.regulation_tags = json.dumps(tags)
            s.add(row)
            result.changed.append(doc.url)
        else:
            result.unchanged.append(doc.url)
    s.commit()
    log.info(
        "diff new=%d changed=%d unchanged=%d",
        len(result.new),
        len(result.changed),
        len(result.unchanged),
    )
    return result
