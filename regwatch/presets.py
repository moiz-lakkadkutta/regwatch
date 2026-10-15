"""Example stores ("presets"): profiles whose results are computed ahead of time.

Layout under the seed directory (`Settings.seed_dir`):

- `stores/<slug>.json`: `{"slug", "name", "blurb", "default"?, "profile": {...}}`. Exactly one store
  is `"default": true`; its profile must equal `profile.json` and its results are the
  `applicability` of each `change-*.json` (so the demo's first screen is the recorded seed run).
- `stores/<slug>.applicability.jsonl` (every other store): one line per seed change, keyed by
  `(document_url, instrument)`, with the change files' payload shape: `model`, `cost_usd` and
  `applicability` {decision, reasons, todos}. `regwatch classify-presets` appends the missing lines;
  `regwatch seed` loads them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from regwatch.config import CATEGORIES, EU_COUNTRIES, PLATFORMS
from regwatch.schemas import Applicability, Profile, RegChange

log = logging.getLogger(__name__)

STORES_DIRNAME = "stores"
APPL_SUFFIX = ".applicability.jsonl"
# Kept out of store slugs: the switcher's "your own profile" entry.
RESERVED_SLUGS = frozenset({"mine"})

ChangeKey = tuple[str, str]  # (document_url, instrument)


class StoreFile(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    name: str = Field(min_length=1)
    blurb: str = Field(min_length=1)
    default: bool = False
    profile: Profile

    @field_validator("slug")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_SLUGS:
            raise ValueError(f"store slug {v!r} is reserved")
        return v

    @field_validator("profile")
    @classmethod
    def _profile_keys(cls, p: Profile) -> Profile:
        """Only the form's own answers, so a preset is a profile a visitor could have saved."""
        if p.establishment_country not in EU_COUNTRIES or not set(p.ship_to) <= set(EU_COUNTRIES):
            raise ValueError("countries must be EU_COUNTRIES codes")
        if not p.categories or not set(p.categories) <= {k for k, _ in CATEGORIES}:
            raise ValueError("categories must be CATEGORIES keys")
        if p.platform not in {k for k, _ in PLATFORMS}:
            raise ValueError("platform must be a PLATFORMS key")
        return p


@dataclass(frozen=True)
class Store:
    file: StoreFile
    path: Path

    @property
    def slug(self) -> str:
        return self.file.slug

    @property
    def appl_path(self) -> Path:
        return self.path.with_name(f"{self.slug}{APPL_SUFFIX}")


@dataclass(frozen=True)
class SeedChange:
    """One `change-*.json` of the seed: the change and the default store's applicability."""

    path: Path
    change: RegChange
    applicability: Applicability
    payload: dict[str, Any]

    @property
    def key(self) -> ChangeKey:
        return (self.change.document_url, self.change.instrument)


@dataclass(frozen=True)
class PresetEntry:
    """One line of a store's jsonl."""

    key: ChangeKey
    applicability: Applicability
    model: str
    cost_usd: float


def load_stores(seed_dir: Path) -> list[Store]:
    """Every `stores/<slug>.json`, the default store first, then by file name. [] without a
    stores directory (an older seed: `profile.json` alone). Raises ValueError on a bad set."""
    root = seed_dir / STORES_DIRNAME
    if not root.is_dir():
        return []
    stores: list[Store] = []
    for path in sorted(root.glob("*.json")):
        file = StoreFile.model_validate_json(path.read_text(encoding="utf-8"))
        if path.stem != file.slug:
            raise ValueError(f"{path.name}: slug {file.slug!r} must match the file name")
        stores.append(Store(file=file, path=path))
    if not stores:
        return []
    defaults = [st for st in stores if st.file.default]
    if len(defaults) != 1:
        raise ValueError(f'{root}: exactly one store must be "default": true')
    demo = Profile.model_validate_json((seed_dir / "profile.json").read_text(encoding="utf-8"))
    if defaults[0].file.profile != demo:
        raise ValueError(f"{defaults[0].path.name}: default store profile must equal profile.json")
    return defaults + [st for st in stores if not st.file.default]


def load_seed_changes(seed_dir: Path) -> list[SeedChange]:
    """Every `change-*.json` in file-name order (the order `regwatch seed` stores them in)."""
    out: list[SeedChange] = []
    for path in sorted(seed_dir.glob("change-*.json")):
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            SeedChange(
                path=path,
                change=RegChange.model_validate(payload["change"]),
                applicability=Applicability.model_validate(payload["applicability"]),
                payload=payload,
            )
        )
    return out


def read_entries(path: Path) -> dict[ChangeKey, PresetEntry]:
    """A store's jsonl by (document_url, instrument); {} when the file does not exist yet.
    A key recorded twice keeps its first line (appends never overwrite)."""
    if not path.exists():
        return {}
    entries: dict[ChangeKey, PresetEntry] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        key = (raw["document_url"], raw["instrument"])
        if key in entries:
            log.warning("%s:%d: duplicate entry for %s [%s]; kept the first", path, n, *key)
            continue
        entries[key] = PresetEntry(
            key=key,
            applicability=Applicability.model_validate(raw["applicability"]),
            model=str(raw.get("model", "")),
            cost_usd=float(raw.get("cost_usd", 0.0)),
        )
    return entries


def append_entry(
    path: Path, change: RegChange, appl: Applicability, *, model: str, cost_usd: float
) -> None:
    """Append one grounded line. Refuses (ValueError) an ungrounded to-do — never stored."""
    if appl.ungrounded_todos(change):
        raise ValueError(f"to-do quote not grounded in obligations: {change.instrument}")
    line = {
        "document_url": change.document_url,
        "instrument": change.instrument,
        "model": model,
        "cost_usd": round(cost_usd, 6),
        "applicability": appl.model_dump(mode="json"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
