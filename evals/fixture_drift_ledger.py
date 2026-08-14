"""The known-drift ledger: a ratchet, not an allowlist.

Every canned fixture in this repo predates the check that compares it to the
platform, and most of them disagree with it. A guard that went red on all of
that on day one would have been turned off on day one, so the drift that
exists at introduction is recorded here and the check fails only on drift
that is NOT recorded. That much is an ordinary allowlist.

What makes it a ratchet is the second rule: an entry that is no longer
observed also fails, with an instruction to delete it. So the ledger can
only shrink. Fixing a fixture forces a line out of this file in the same PR,
and nothing can quietly regrow.

Entries are keyed by ``(scenario, tool, path, kind)`` and deliberately carry
no observed values. A gauge that wobbles between runs is the same unfixed
drift, and re-blessing the file on every wobble would turn it into a rubber
stamp — which is how this class of guard usually dies.

Regenerate with ``make fixture-drift-bless`` (never by hand): it needs a
live platform, and hand-editing would let an entry in that no live run ever
justified.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from evals.fixture_drift import Drift

LEDGER_PATH: Final[Path] = Path(__file__).resolve().parent / "fixture-drift-ledger.json"

DriftKey = tuple[str, str, str, str]


def load_ledger(path: Path | None = None) -> frozenset[DriftKey]:
    """The recorded drift keys. A missing ledger is an empty one — strictest."""
    target = path or LEDGER_PATH
    if not target.exists():
        return frozenset()
    payload = json.loads(target.read_text())
    return frozenset(
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in payload.get("known_drift", [])
        if isinstance(row, list) and len(row) == 4
    )


def dump_ledger(drifts: Iterable[Drift], path: Path | None = None) -> int:
    """Write the ledger from an observed drift set. Returns the entry count."""
    target = path or LEDGER_PATH
    keys = sorted({drift.key for drift in drifts})
    target.write_text(
        json.dumps(
            {
                "_comment": (
                    "Known canned-vs-live fixture drift, recorded when the drift check "
                    "was introduced. This file may only SHRINK: evals/fixture_drift_ledger.py "
                    "fails on drift not listed here AND on entries listed here that are no "
                    "longer observed. Regenerate with `make fixture-drift-bless` against the "
                    "pinned platform; never hand-edit."
                ),
                "_fields": ["scenario", "tool", "path", "kind"],
                "known_drift": [list(key) for key in keys],
            },
            indent=2,
        )
        + "\n"
    )
    return len(keys)


def classify(
    drifts: Iterable[Drift], ledger: frozenset[DriftKey]
) -> tuple[tuple[Drift, ...], tuple[DriftKey, ...]]:
    """Split observed drift into ``(new, stale_ledger_entries)``.

    ``new`` is drift the ledger does not record — the check's actual subject.
    ``stale`` is recorded drift that no longer occurs, which means a fixture
    was fixed and its line here has to go.
    """
    observed = {drift.key for drift in drifts}
    new = tuple(drift for drift in drifts if drift.key not in ledger)
    stale = tuple(sorted(ledger - observed))
    return new, stale
