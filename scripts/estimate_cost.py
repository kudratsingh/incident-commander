#!/usr/bin/env python3
"""Estimate API cost from eval trace JSONL — an ESTIMATE, never the ledger.

Trace-derived cost is a structural LOWER BOUND on billed cost: traces are APPEND-ONLY across
invocations (``evals/tracing.py``, F-002), so the aggregation groups by ``invocation_id`` and
older records fall under ``pre-invocation-id``; tracing is opt-in via ``EVAL_TRACE_DIR``, so
a direct ``evals.runner`` call spends real money and records nothing; and only the final
response's ``usage`` is captured. Run 001 measured the gap at ~1.9x (study/runs.jsonl). The
console stays authoritative; this only prices records right: cache-write at 1.25x input,
cache-read at 0.1x.

Usage:
    uv run python scripts/estimate_cost.py [--since ISO8601] [trace_dir]

``trace_dir`` also accepts ``evals/runs/<invocation_id>/traces``, including a run killed
mid-suite with no ``report.json`` (ADR 0017); those calls were billed.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

# $/token, as (input, output, cache_read, cache_write). Verify against
# https://platform.claude.com/docs/en/pricing before editing. NOT
# ``incident_commander.llm.pricing``, which pins exact ids for a live budget: this is
# forensic and matches by tier substring. Overlaps must agree to the cent —
# tests/unit/test_estimate_cost.py::test_rates_agree_with_the_pinned_pricing_module.
RATES: Final[dict[str, tuple[float, float, float, float]]] = {
    "sonnet": (3.0e-6, 15.0e-6, 0.30e-6, 3.75e-6),
    "haiku": (1.0e-6, 5.0e-6, 0.10e-6, 1.25e-6),
    "opus": (5.0e-6, 25.0e-6, 0.50e-6, 6.25e-6),
}


# Group key for records predating the invocation_id stamp. Must stay byte-identical to
# scripts/format_traces.PRE_INVOCATION_ID and evals/runner.py::_archive_trace_slice.
PRE_INVOCATION_ID: Final[str] = "pre-invocation-id"


def _aware(raw: str) -> datetime:
    """Parse an ISO-8601 stamp, defaulting a naive one to the tracer's UTC.

    Trace stamps are aware; a date-only ``--since`` is naive, and comparing them raised
    ``TypeError``. Both sides come through here.
    """
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _tier(model: str) -> str:
    """Which price row a model id falls under; anything unrecognised is priced as sonnet."""
    for name in RATES:
        if name in model:
            return name
    return "sonnet"


def main() -> int:
    """Total the traced LLM calls by invocation, model tier and role, and price them."""
    argv = sys.argv[1:]
    since = None
    if "--since" in argv:
        i = argv.index("--since")
        since = _aware(argv[i + 1])
        del argv[i : i + 2]
    trace_dir = Path(argv[0]) if argv else Path("evals/traces")

    agg: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    trace_files = sorted(trace_dir.glob("*.jsonl"))
    if not trace_files:
        # A silent $0.00 reads like "the run was free" rather than "you
        # pointed me at the wrong directory" — say which it is.
        print(f"no trace files (*.jsonl) under {trace_dir}")
        return 0
    for path in trace_files:
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "llm":
                continue
            stamp = rec.get("timestamp")
            if since and stamp:
                when = _aware(str(stamp))
                if when < since:
                    continue
            usage = (rec.get("response") or {}).get("usage") or {}
            key = (
                str(rec.get("invocation_id") or PRE_INVOCATION_ID),
                _tier((rec.get("request") or {}).get("model", "")),
                str(rec.get("role", "?")),
            )
            row = agg[key]
            row[0] += 1
            # `or 0`, not just the .get default: the Usage cache fields are Optional, so the
            # JSON carries nulls and `row[n] += None` aborts the audit (A-12).
            row[1] += usage.get("input_tokens", 0) or 0
            row[2] += usage.get("output_tokens", 0) or 0
            row[3] += usage.get("cache_read_input_tokens", 0) or 0
            row[4] += usage.get("cache_creation_input_tokens", 0) or 0

    per_invocation: dict[str, float] = defaultdict(float)
    total = 0.0
    print(f"{'invocation':14s} {'tier/role':34s} {'calls':>5s} {'cost':>8s}")
    for (inv, tier, role), (n, inp, out, cr, cw) in sorted(agg.items()):
        p_in, p_out, p_cr, p_cw = RATES[tier]
        cost = inp * p_in + out * p_out + cr * p_cr + cw * p_cw
        per_invocation[inv] += cost
        total += cost
        print(f"{inv:14s} {tier + '/' + role:34s} {n:5d} ${cost:7.2f}")
    if len(per_invocation) > 1:
        print("\nper invocation:")
        for inv, cost in sorted(per_invocation.items()):
            print(f"  {inv:14s} ${cost:7.2f}")
    print(f"\nESTIMATE (lower bound): ${total:.2f}")
    print("The console is the ledger. Record both numbers in study/runs.jsonl;")
    print("console actuals are authoritative for efficiency (property 6).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
