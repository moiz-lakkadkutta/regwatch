"""`regwatch classify-presets`: fill each example store's jsonl with the classifier's results.

For every non-default store × every seed change without a line in the store's
`<slug>.applicability.jsonl`, run the classifier (CLASSIFIER, Ultra) on (change, store profile),
check `Applicability.ungrounded_todos()` is empty and append one line. Failures and refusals write
nothing, so a rerun retries them; a rerun after new change files only classifies those. Usage and
cost are logged per model call (understand.classify) and per store.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from regwatch.config import Settings
from regwatch.presets import append_entry, load_seed_changes, load_stores, read_entries
from regwatch.understand.classify import classify
from regwatch.understand.llm_backend import LLMBackend, ModelLedger

log = logging.getLogger(__name__)


@dataclass
class StoreTally:
    slug: str
    n_existing: int = 0
    n_applies: int = 0
    n_does_not: int = 0
    n_unclear: int = 0
    n_failed: int = 0
    n_refused: int = 0
    cost_usd: float = 0.0

    @property
    def n_added(self) -> int:
        return self.n_applies + self.n_does_not + self.n_unclear

    def line(self) -> str:
        return (
            f"{self.slug}: {self.n_added} added ({self.n_applies} apply · {self.n_does_not} "
            f"don't · {self.n_unclear} unclear) · {self.n_existing} already there · "
            f"{self.n_failed} failed · {self.n_refused} refused ungrounded · ${self.cost_usd:.4f}"
        )


@dataclass
class PresetSummary:
    stores: list[StoreTally] = field(default_factory=list)
    n_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def lines(self) -> list[str]:
        total = (
            f"classify-presets: {sum(t.n_added for t in self.stores)} added · "
            f"{self.n_calls} model calls · {self.prompt_tokens}/{self.completion_tokens} tokens · "
            f"${self.cost_usd:.4f}"
        )
        return [t.line() for t in self.stores] + [total]


def classify_presets(
    settings: Settings,
    *,
    backend: LLMBackend,
    seed_dir: Path | None = None,
    only: Sequence[str] = (),
) -> PresetSummary:
    """Fill the gaps of every non-default store (or the slugs in `only`); returns the tally."""
    seed_dir = seed_dir or settings.seed_dir
    stores = load_stores(seed_dir)
    unknown = set(only) - {st.slug for st in stores}
    if unknown:
        raise ValueError(f"unknown store(s): {', '.join(sorted(unknown))}")
    changes = load_seed_changes(seed_dir)
    ledger = ModelLedger.for_classifier(settings)
    summary = PresetSummary()
    for store in stores:
        if store.file.default or (only and store.slug not in only):
            continue
        tally = StoreTally(slug=store.slug)
        summary.stores.append(tally)
        have = set(read_entries(store.appl_path))
        for sc in changes:
            if sc.key in have:
                tally.n_existing += 1
                continue
            result = classify(
                sc.change, store.file.profile, backend=backend, settings=settings, ledger=ledger
            )
            tally.cost_usd += result.cost_usd
            appl = result.applicability
            if appl is None:
                tally.n_failed += 1
                continue
            # The grounding rule, enforced in code right before anything is stored.
            if appl.ungrounded_todos(sc.change):
                log.error(
                    "classify-presets %s: refusing ungrounded to-dos %s", store.slug, sc.path.name
                )
                tally.n_refused += 1
                continue
            append_entry(
                store.appl_path,
                sc.change,
                appl,
                model=result.model,
                cost_usd=result.cost_usd,
            )
            have.add(sc.key)
            if appl.decision == "applies":
                tally.n_applies += 1
            elif appl.decision == "does_not":
                tally.n_does_not += 1
            else:
                tally.n_unclear += 1
            log.info(
                "classify-presets %s %s: %s · %d to-do(s) · $%.4f",
                store.slug,
                sc.path.name,
                appl.decision,
                len(appl.todos),
                result.cost_usd,
            )
        log.info("%s", tally.line())
    summary.n_calls = ledger.n_calls
    summary.prompt_tokens = ledger.prompt_tokens
    summary.completion_tokens = ledger.completion_tokens
    summary.cost_usd = ledger.cost_usd
    return summary
