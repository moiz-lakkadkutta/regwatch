"""Split a long EU legal act into sections (per article / amendment point). Pure: no model calls.

A 100k-char act such as Directive (EU) 2023/2673 hides its one consumer-facing change (Art. 11a,
the withdrawal button) inside `Article 1, point (3)`. Normalising and classifying each section on
its own keeps the model focused. Every section's text is a verbatim slice of the markdown, except
that an amendment-point section starts with the article's intro (capped) so the model knows which
act is being amended. Amendment points are never merged with each other. Grounding is
unchanged: quotes are still validated against the FULL document markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from regwatch.config import Settings
from regwatch.schemas import Document

INTRO_CAP = 500
_EURLEX_HOST = "eur-lex.europa.eu"
_ADOPTED = re.compile(
    r"HAVE ADOPTED THIS (?:DIRECTIVE|REGULATION|DECISION)[^\n]*\n?", re.IGNORECASE
)
_HEADING = re.compile(r"^(?:Article (\d+[a-z]?)[ \t]*|(ANNEX\b.*?)[ \t]*)$", re.MULTILINE)
_POINT = re.compile(
    r"(?:^|(?<=[\s:]))\((\d+)\) ?"
    r"(?=the following|Articles?\b|in Articles?\b|Annex|point|paragraph)",
    re.MULTILINE,
)
_BLANK = re.compile(r"(\n[ \t]*\n)")
_LABEL_CAP = 40


@dataclass(frozen=True)
class Section:
    label: str
    text: str


@dataclass(frozen=True)
class _Piece:
    label: str
    intro: str  # prepended to the body; empty for plain pieces
    body: str

    @property
    def text(self) -> str:
        return f"{self.intro}\n\n{self.body}" if self.intro else self.body


def is_long_act(document: Document, settings: Settings) -> bool:
    """A EUR-Lex act (ELI or legal-content URL) longer than `act_split_min_chars`."""
    parsed = urlparse(document.url)
    host = (parsed.hostname or "").lower()
    if host != _EURLEX_HOST and not host.endswith("." + _EURLEX_HOST):
        return False
    if "/eli/" not in parsed.path and "/legal-content/" not in parsed.path:
        return False
    return len(document.markdown) > settings.act_split_min_chars


def split_act(markdown: str, *, max_chars: int = 15_000, min_chars: int = 3_000) -> list[Section]:
    """Split the operative part of an act into sections, in document order."""
    if not markdown.strip():
        return []
    adopted = _ADOPTED.search(markdown)
    body = markdown[adopted.end() :] if adopted else markdown
    pieces = _merge(_top_level_pieces(body), min_chars, max_chars)
    sections: list[Section] = []
    for piece in pieces:
        sections.extend(_cap(piece, max_chars))
    return sections or [Section("Act", body.strip())]


def _top_level_pieces(body: str) -> list[_Piece]:
    heads = list(_HEADING.finditer(body))
    pieces: list[_Piece] = []
    first = heads[0].start() if heads else len(body)
    if body[:first].strip():
        pieces.append(_Piece("Introductory provisions", "", body[:first].strip()))
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        text = body[head.start() : end].strip()
        if head.group(1):
            pieces.extend(_split_points(f"Article {head.group(1)}", text))
        else:
            pieces.append(_Piece(head.group(2)[:_LABEL_CAP], "", text))
    return pieces


def _split_points(label: str, text: str) -> list[_Piece]:
    points: list[re.Match[str]] = []
    for m in _POINT.finditer(text):
        if int(m.group(1)) == len(points) + 1:  # amendment points count 1, 2, 3 …
            points.append(m)
    if len(points) < 2:
        return [_Piece(label, "", text)]
    intro = text[: points[0].start()].strip()[:INTRO_CAP]
    pieces: list[_Piece] = []
    for i, m in enumerate(points):
        end = points[i + 1].start() if i + 1 < len(points) else len(text)
        pieces.append(
            _Piece(f"{label}, point ({m.group(1)})", intro, text[m.start() : end].strip())
        )
    return pieces


def _merge(pieces: list[_Piece], min_chars: int, max_chars: int) -> list[_Piece]:
    merged: list[_Piece] = []
    run: list[_Piece] = []

    def flush() -> None:
        if run:
            merged.append(_join(run))
            run.clear()

    for piece in pieces:
        if piece.intro or len(piece.text) >= min_chars:  # amendment points stand alone
            flush()
            merged.append(piece)
            continue
        if run and len(_join([*run, piece]).text) > max_chars:
            flush()
        run.append(piece)
    flush()
    return merged


def _join(run: list[_Piece]) -> _Piece:
    if len(run) == 1:
        return run[0]
    label = f"{run[0].label} – {run[-1].label}"
    return _Piece(label, "", "\n\n".join(p.text for p in run))


def _cap(piece: _Piece, max_chars: int) -> list[Section]:
    # intro + "\n\n" + part stays within max_chars + INTRO_CAP
    budget = max_chars + INTRO_CAP - len(piece.intro) - 2 if piece.intro else max_chars
    if len(piece.text) <= max_chars + (INTRO_CAP if piece.intro else 0):
        return [Section(piece.label, piece.text)]
    parts = _pack(piece.body, budget)
    return [
        Section(f"{piece.label} (part {i})", _Piece("", piece.intro, part).text)
        for i, part in enumerate(parts, start=1)
    ]


def _pack(text: str, budget: int) -> list[str]:
    """Greedy-pack paragraphs into parts of at most `budget` chars; hard-cut oversize paragraphs.

    Never returns a blank part.
    """
    parts: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            parts.append(current.strip())
        current = ""

    for token in _BLANK.split(text):
        while len(token) > budget:  # a single paragraph longer than the budget
            flush()
            if token[:budget].strip():
                parts.append(token[:budget].strip())
            token = token[budget:]
        if len(current) + len(token) > budget:
            flush()
        if token.strip() or current:  # drop separators that would start a part
            current += token
    flush()
    return parts
