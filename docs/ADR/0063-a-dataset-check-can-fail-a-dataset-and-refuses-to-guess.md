# ADR 0063: A dataset check can fail a dataset, and refuses to guess

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh

## Context and problem statement

WP-15.3 is the last thing between the training export ([ADR 0057](0057-the-training-export-carries-refs-and-labels-and-no-prose.md))
and a later training stage. Plan 04 § WP-15.3 and plan 03 § 16.4 name eight things it has to find:
missing tool results, incomplete trajectories, leaked hidden truth, scenario drift, duplicates,
invalid rewards, action/result mismatches, and holdout contamination by `template_id`.

The list is the easy part. Four questions decide whether the checker is worth having, and each has a
comfortable wrong answer:

1. **Does it fail a dataset, or annotate one?** A quality report is cheaper to write and easier to
   ship green.
2. **What does it do with a problem it cannot classify?** Every default here is a temptation to call
   it harmless.
3. **How does it know what the answer key looks like?** The obvious implementation is a list of
   forbidden key names.
4. **Is the holdout check a second layer, or the export's own refusal called twice?**

## Decision drivers

* An incomplete trajectory or a leaked answer key in a training set is **invisible once training
  starts**. There is no later stage that catches it; the export is the last readable form.
* `LESSONS.md` 2026-09-07 / WO-R2-138: *"`failure_class` in a run archive is a hint, never a
  verdict"*, and auto-labels on a red result lean benign — two live reds in one day were classified
  as harmless and one of them was a real agent finding.
* WO-R3-185's argument for the agent-visible allow-list: a hand-maintained exclusion list fails by
  **omission**, which is silent, and the field added next year is the one nobody adds to the list.
* Invariant 6: what an agent DID is what the platform audit log says, not what the trajectory claims.
* Invariant 9: the checker reads evidence. It writes nothing and moves nothing.

## Decision

**1. It can fail a dataset, and the exit code is the interface.** `evals/dataset_checks.py` exits 1
on a finding that matters and 0 otherwise; `make dataset-checks` is the operator's form. Every
finding names the record it is about — the `trajectory_id`, or the line number when the line has no
usable id — so the report is actionable rather than a count.

**2. `unclassified` is a real severity, and it fails the dataset like `blocking` does.** Three
severities: `blocking` (the dataset is not usable), `unclassified` (something is wrong or unknown
and the checker will not pick which), `advisory` (something a reader of the dataset needs to know —
never a defect). The
failing set is `{blocking, unclassified}`, in code, because *"nobody could classify it" is not
evidence that it is harmless* — that inference is exactly the auto-label failure of 2026-09-07 in a
new place. Four shapes land there on purpose: a line the trajectory model refuses for no visible
reason (a schema bump, a hand edit and a corrupt write are indistinguishable from the outside), a
reward carrying another `spec_version` (its components mean whatever that spec said, so their ranges
cannot be checked here), a scenario the corpus does not hold (renamed, deleted, or another
checkout's — and with nothing to compare against, drift cannot be measured either way), and an audit
window that was not fully scanned (a partial window cannot show the ABSENCE of an action).

**3. The hidden-truth key set is computed, never listed.** It is *what
`export.LabelRecord` carries and `export.TrajectoryRecord` does not*, walked through both model
graphs including nested models. A label field added tomorrow joins the set on its own; a key living
on both sides cannot be evidence of a leak, so the derivation cannot produce a false positive out of
a legitimate training field. A test pins the derived set against the key list
`tests/unit/test_ground_truth_never_leaks.py` already maintains, so the two cannot drift apart.

By VALUE the check reads the trajectory's own answer key out of the labels file beside it and looks
for it on the training line, at every depth, with two scopings that come from the same projection
rather than from tuning:

* **What the agent could read is not a leak.** A value the scenario's `agent_visible()` projection
  already carries is excluded: a real component name is usually in the alert *and* in
  `affected_components`, and the agent naming the consumer group it probed is the agent working.
  Without this the check fired 191 times on the shipped trace store (see the addendum), and a check
  that cries wolf that often is worse than no check.
* **The line's own benchmark keys are out of scope, and reported instead.** `scenario`,
  `template_id`, `family`, `difficulty` and `benchmark_split` are on the line by ADR 0057's decision
  and the agent never saw one, so a template named after its fault (`redis_saturation`) is a fact
  about the CORPUS. Blocking it would fail every dataset that corpus can produce; saying nothing
  would let a training stage key on the name. It is an advisory that names the key.

A root-cause label is a leak everywhere else except the one `category` slot where it is the agent's
own ranking rather than the answer. That slot is one named key, pinned to `RankedCategory` and
`CandidateExport` by a test — not an exclusion list, and not a guess about which paths are safe.

**4. The holdout check is a genuinely second layer.** It is computed from the corpus and the exported
lines, reusing `export.holdout_template_ids` (a projection of the corpus) and never
`export._refuse_holdout` (the export's refusal). That is what lets it fail where layer one passed:
a template moved into `holdout` *after* an export was written contaminates the export, and the export
cannot notice that, because it already ran. A test breaks the export's refusal and asserts the check
still fires.

**5. Incompleteness is structural, twice.** The line carries `outcome.complete`, which is the
export's own `scenario_start`-and-`scenario_end` boundary check. With `TRACE_DIR=` the checker
re-derives the same fact from the append-only trace store, and the two disagreeing is itself a
blocking finding — the line's claim about itself is checked rather than trusted.

**6. Manifest integrity is a ninth class, beyond the plan's eight.** The checker reads the manifest to
find the files, so the manifest's digests, counts, template sets and the join between the two files
are checked against the files themselves. Without it the whole checker rests on trusting the one
document a later stage reads *instead of* the data.

## Consequences

* A dataset with no audit windows cannot be checked on action correctness at all, and that is stated
  as an advisory with the reason rather than passing quietly. Offline and recorded exports are in
  that shape by construction; `AUDIT=<json>` is the way in for a live run's window.
* `unclassified` failing the build means a schema bump to the export makes `make dataset-checks` red
  on older exports until someone looks. That is the intended cost: the alternative is a checker that
  goes quiet exactly when the format moved under it.
* The value-side leak check needs the labels file, so the checker is evaluator-side tooling and
  always will be. It reads the answer key in order to hunt for it; nothing it prints carries one.
* Nine classes and three severities is more surface than the plan's eight names. The extra two
  (manifest integrity, and `advisory` as a statement rather than a defect) exist so that "clean"
  never means "that layer did not run".
* A leak of an `affected_components` value that the alert also names goes undetected BY VALUE. The
  key-name layer still covers it, which is the layer WO-R3-185 argues is the structural one; the
  value layer is the belt, not the braces.

## Alternatives considered

* **A report with no exit code.** Rejected: WP-15.3's own rationale is that the failure it guards
  against is invisible later, and a warning nobody is obliged to read is how it stays invisible.
* **Classify an unknown finding as advisory and move on.** Rejected — this is the decision the
  lessons ledger exists to prevent. The benign default is the expensive one.
* **A list of forbidden key names.** Rejected for WO-R3-185's reason. The failure mode is omission,
  which no test catches, because the omitted field is by definition the one nobody thought of.
* **Call the export's holdout refusal from the checker.** Rejected: it would pass in exactly the case
  the second layer exists for, and two layers that share a code path are one layer.
* **Ban every root-cause label string from the training line.** Rejected: the agent's own ranking is
  full of them and is the data. The scoping is by JSON path, not by string.

## Addendum: what the first run against the real trace store changed

Written against the fixture, the checker reported 516 blocking findings over a 168-line export
of `evals/traces/`. Four of the five shapes were the checker's own fault, and each one is now a
decision above rather than a tuning knob:

* **34 false `incomplete_trajectory`.** One eval run writes ONE `invocation_id` into every
  scenario's trace file, so keying the boundary map by that id collapsed 31 trajectories into
  whichever file was read last. The key is the `trajectory_id`, which is what a trajectory is.
* **283 false `action_result_mismatch`.** The older traces predate the tracer's `record_id`, so
  every observation on those lines carries the same blank id and resolving an action through it
  bound it to the last reading. An id that identifies nothing is now one `missing_result` finding
  per line — which is the true statement — and the per-action comparison is skipped for it.
* **191 false `leaked_hidden_truth`.** `affected_components` holds real component names that the
  alert also names, so the value scan flagged the agent for naming the consumer group it probed.
  Scoped by `Scenario.agent_visible()`.
* **12 more.** A template named after its own fault (`redis_saturation`) put a root-cause label in
  the line's `scenario` field. Advisory, not blocking.

What remains on that store is true and is the checker earning its place: 85 lines whose
action/result binding is unidentifiable, 3 trajectories with a `scenario_start` and no
`scenario_end`, and 11 sets of canned replays that are the same run several times.

The general lesson, for the next checker: **a check written only against a fixture measures the
fixture.** Four of these five would have shipped green.
