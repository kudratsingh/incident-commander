# ADR 0061: A threshold is declared with the split it was set on, and the holdout is refused at construction

* Status: accepted
* Date: 2026-09-18
* Decider: Kudrat Singh

## Context

Plan 02 § 15 (WP-13.1) asks for the adaptive ladder's escalation signals — top-1 confidence low,
top1/top2 margin narrow, selector uncertainty high, candidate disagreement high, contradictory
evidence, a remediation attempt failed, confidence still low after K probes — as *config
thresholds* rather than as numbers in the code. The point is not tunability for its own sake: it
is that the operating point a result was produced under becomes a reported number instead of a
constant somebody would have to go and read.

Three facts shape the design.

**The holdout promise is only as good as its enforcement.** Plan 03 § 4 says the holdout is never
tuned against, and every instance of a held-out template is held out. The buildout already keeps
that promise in two places mechanically — the loader refuses a `template_id` claimed by two splits,
and the training export refuses a held-out template by name ([ADR 0057](0057-the-training-export-carries-refs-and-labels-and-no-prose.md)).
A threshold tuned on the holdout would break the same promise in a place nobody looks, and it would
do it silently: the number is a float, and a float carries no history.

**The 0.7 remediate bar is not one of these thresholds.** It lives in `agent/investigation.py`'s
handoff gate and has decided every green run in this campaign. Plan 02 § 16 makes it a *reported*
operating point, re-examined per model in the phase-close protocol, never tuned. Moving it while
adding an adaptive ladder would move every arm's numbers at once, and nobody reading the report
would be able to tell which change did it.

**Nothing has been tuned yet.** No sweep has run — WP-13.2's is the first, and every paid run is
deferred under standing instruction O-22. So the honest provenance of every default in this packet
is "declared, no run behind it", which is a different claim from "set on dev".

## Decision

**1. A threshold default is a declaration, and a declaration carries its split.**
`agent/strategies/policy.py` holds one `ThresholdDefault` row per threshold: the value, the
`EscalationSignal` it decides, the `ThresholdSplit` the value was set on, the source that set it,
and the reason. `UNCERTAINTY_DEFAULTS` is that table. There is nowhere else in the module a number
may be written, and `tests/unit/test_uncertainty_policy.py` proves it with an AST scan — every
numeric literal in the module must be a `ThresholdDefault(value=…)` argument, and the scan is shown
to catch a planted one.

**2. `holdout` is refused at construction, not reviewed.** `ThresholdDefault.__post_init__` calls
`assert_tuning_split_allowed`, which raises `HoldoutTunedThresholdError` for any split outside
`TUNABLE_SPLITS` (`dev`, `validation`, `untuned`). A default derived from a holdout run cannot be
declared honestly and cannot be declared dishonestly without lying about the split in a reviewable
diff. The same function is the one a later sweep calls with the split of the run it tuned on, so
the refusal sits on the path that would otherwise introduce the fault.

**3. `untuned` is a fourth, real provenance value.** The three benchmark splits are
`evals.scenarios.schema.BenchmarkSplit`'s, pinned to it by a test rather than imported — nothing
under `agent/` imports the harness. `untuned` says "no run set this number", which a report must be
able to say, because it is true of all eight defaults today and will stay true of any threshold a
sweep does not reach.

**4. Configuration carries the override; the module carries the default.** The eight `Settings`
fields (`UNCERTAINTY_*`) all default to `None`, meaning "the declared default". A number in
`config.py` would be a second declaration with no split attached, and a split attached in
`config.py` could not be read by the policy at all: `config` importing the policy module closes an
import cycle through `tools/mcp_client.py`. So the override lives at the edge and the provenance
lives with the value, which is also how `StrategyKnobs` is wired (nothing under `agent/` reads
`Settings`).

**5. An overridden threshold reports no split.** `provenance_rows()` renders the live value, the
declared default, whether they are equal, the declared split and the source. When an operator has
overridden a threshold, `live_value_split` is `None`. Nobody can say which split a value typed into
an environment came from, and reporting `untuned` there would be a false claim about provenance
rather than an absent one.

**6. The remediate bar stays exactly where it is.** No knob, no re-declaration, and no default in
this table may equal it — three properties pinned by tests. The policy module does not reference it
at all, which the strategies-seam scan already enforces
([ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md)), and `docs/eval-methodology.md`
states its value as the reported operating point plan 02 § 16 asks for.

**7. A signal with no measurement is UNMEASURED.** Four signals are read off `RunState` — the two
confidence readings, and `remediation_attempt_failed`, which counts the attempt records the
remediation loop appends under `planner_context.ATTEMPT_FAILED_MARKER`
([ADR 0056](0056-a-retry-earns-its-attempt-by-reinvestigating.md)), imported rather than
re-spelled. Three need something only an arm produces (the selector's `uncertainty`, the candidate
set's disagreement and self-contradiction). `evaluate` reports those as unmeasured rather than as
not fired, so a report can never read "the ladder did not escalate" when the truth is "nothing
measured it".

## Considered and rejected

* **One number per signal, in `config.py` with its default.** The obvious shape, and it puts the
  value where the operator looks. Rejected: the split then has nowhere to live that the policy can
  read (the import cycle above), and a default in configuration is a number with no provenance —
  exactly what this packet exists to remove.
* **A module constant per threshold, documented in a comment.** Cheaper, and the repo does this
  elsewhere (`DEFAULT_MAX_REMEDIATION_ATTEMPTS`). Rejected here because a comment is not checkable:
  the AST scan can prove a number sits inside a row that demands a split; it cannot prove a comment
  above a constant is true.
* **Reuse `BenchmarkSplit` directly.** Rejected: `agent/` importing `evals/` inverts the dependency
  the whole repo rests on. The vocabulary is pinned by a test instead, which fails if either side
  gains a member.
* **Let a holdout-derived default through with a warning.** Rejected for the reason ADR 0057 gives
  for the export's hard refusal: a promise resting on somebody noticing a warning is not a promise.

## Consequences

* Every escalation number in the ladder is reportable with its provenance, and the report's
  threshold table is generated from the same rows the code compares against.
* WP-13.2's sweep has one job it cannot skip: when it sets a default, it declares the split of the
  run that set it, and the refusal fires if that run was on the holdout.
* Eight new environment variables exist that no shipped arm reads. That is deliberate and pinned:
  a test asserts nothing under `src/` imports the policy module yet, which is what makes the canned
  baseline provably unmoved by this packet.
* `untuned` will be visible in reports until sweeps replace it. That is the intended embarrassment:
  a declared number that no run has justified should look like one.
