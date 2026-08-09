#!/usr/bin/env python3
"""Estimate API cost from eval trace JSONL — an ESTIMATE, never the ledger.

Trace-derived cost is structurally a LOWER BOUND on billed cost, for reasons
that are properties of the tracer rather than bugs to fix:

* Traces are per-scenario files, APPEND-ONLY across invocations. A re-run
  concatenates a new block of records rather than overwriting the earlier
  attempt's, and every record carries the ``invocation_id`` that keeps the
  attempts distinguishable (``evals/tracing.py``). Mixed-vintage files are
  therefore expected and normal, and the aggregation below groups by that
  id — records predating the stamp fall into ``pre-invocation-id``. This
  bullet used to claim the opposite: until 2026-08-07 the tracer truncated
  on construction, and Run 001's killed first attempt was erased in full by
  its own re-run (study/findings.md F-002). That is what the ~1.9x gap
  below actually measured.
* Tracing is opt-in via ``EVAL_TRACE_DIR``. Any direct ``evals.runner``
  invocation without it spends real money and writes no record at all.
* Only the final response's ``usage`` is captured per call.

Run 001 measured the resulting gap at ~1.9x (see study/runs.jsonl).
Console actuals are authoritative for cost; this script exists to make the
estimate principled — correct per-model rates including cache-write at
1.25x input and cache-read at 0.1x input — not to replace the console.

Usage:
    uv run python scripts/estimate_cost.py [--since ISO8601] [trace_dir]

``trace_dir`` also accepts an archived slice —
``evals/runs/<invocation_id>/traces`` — including one from a run with no
``report.json``, i.e. a run that was killed mid-suite (ADR 0017). Those
calls were billed and count.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Final

# $/token, as (input, output, cache_read, cache_write). Cache write = 1.25x
# input (5-minute TTL); cache read = 0.1x input.
# Verify against https://platform.claude.com/docs/en/pricing before editing.
#
# Deliberately NOT the runtime meter's table. ``incident_commander.llm.pricing``
# pins exact model ids for the two models the agent actually runs and charges
# an unknown id at the most expensive PINNED row so a live budget can never
# under-report. This script is forensic instead: it prices historical trace
# records whose model ids may no longer be pinned anywhere (opus below is not
# in MODEL_PRICING), so it matches by tier substring. Where the two tables
# overlap they must agree to the cent —
# tests/unit/test_estimate_cost.py::test_rates_agree_with_the_pinned_pricing_module
# fails on drift.
RATES: Final[dict[str, tuple[float, float, float, float]]] = {
    "sonnet": (3.0e-6, 15.0e-6, 0.30e-6, 3.75e-6),
    "haiku": (1.0e-6, 5.0e-6, 0.10e-6, 1.25e-6),
    "opus": (5.0e-6, 25.0e-6, 0.50e-6, 6.25e-6),
}


# Group key for records written before the tracer stamped invocation_id.
# Must stay byte-identical to scripts/format_traces.PRE_INVOCATION_ID and to
# the missing-key convention in evals/runner.py::_archive_trace_slice.
PRE_INVOCATION_ID: Final[str] = "pre-invocation-id"


def _tier(model: str) -> str:
    for name in RATES:
        if name in model:
            return name
    return "sonnet"


def main() -> int:
    argv = sys.argv[1:]
    since = None
    if "--since" in argv:
        i = argv.index("--since")
        since = datetime.fromisoformat(argv[i + 1].replace("Z", "+00:00"))
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
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
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
            # `or 0`, not just the .get default: the anthropic Usage cache
            # fields are Optional, so the dumped JSON carries an explicit
            # null for them and `.get(key, 0)` hands back None — `row[n] +=
            # None` then aborts the whole audit on one record (A-12). Same
            # defense llm/client.py already applies when it reads usage.
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
