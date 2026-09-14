"""New vs changed vs unchanged, by content hash. Lands Oct 18."""

from __future__ import annotations

import hashlib


def content_hash(markdown: str) -> str:
    return hashlib.sha256(" ".join(markdown.split()).encode()).hexdigest()
