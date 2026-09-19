# ADR 0057: The training export carries refs, a separate answer key, and no model prose

* Status: accepted
* Date: 2026-09-18
* Decider: Kudrat Singh

## Context and problem statement

WP-15.1 builds the artifact this whole buildout is meant to leave behind: a JSONL export of
the traced trajectories that a later training stage would read (plan 03 § 16.4). It is the
one eval output that *leaves the harness*, and nobody re-reads a training set line by line.
Three things it could carry are each defensible in isolation and each wrong here — the raw
tool output the agent saw, the planner's `reasoning` text, and the evaluator's ground truth —
and a fourth question decides whether the holdout promise means anything: whether a held-out
template reaching the export is an error or something that gets filtered out.

## Decision drivers

* Raw tool output is untrusted data (CLAUDE.md invariant 4). A DLQ payload's text inside a
  training set is adversarial content in the training loop, not a logging detail.
* Ground truth is evaluator-only and graded only in the world it describes (ADR 0038, 0040).
* The trace store is append-only evidence (invariant 9). F-002 is what consuming evidence
  looks like: a re-run erased a killed attempt in full and ~$1.2 of billed work vanished.
* Holdout is by template, enforced in three places — loader, export, report (plan 03 § 4).
  Instance-level holdout lets a later SFT stage memorise a template through its siblings
  (plan 06 D7).
* An export has to be re-derivable, or it cannot support a claim about what a policy saw.
* F-011: the harness has already rewarded the wrong thing once. A second definition of
  anything scored is how that happens.

## Considered options

1. Export the trace records as they are, minus a hand-listed set of secret keys.
2. Export an allow-list projection: refs for observations, structured decisions with no free
   text, labels in a separate file, a manifest naming every exported template, and a hard
   refusal on holdout (chosen).

## Decision outcome

`evals/export.py` renders three files per export, versioned and exclusive-create through
`evals/artifacts.py` (kinds `training_export`, `training_export_labels`,
`training_export_manifest`, all in `evals/exports/`):

* **`…​.jsonl` — the training data.** One line per trajectory, where a trajectory is one
  `invocation_id` in one trace file. It carries the benchmark keys (`template_id`, `seed`,
  `family`, `difficulty`, `benchmark_split`), the run's identity and models, the planner's
  **decisions** (candidate set, selector decision, the emitted action's kind and tool, the
  ranking either side, the per-step bill), the **actions** the agent took with their
  arguments, and each action's **result as a reference** — `(trace_file, observation_id)`
  plus a SHA-256 of a canonical rendering of the content and its byte length. Never the
  content itself, and never an error string.
* **`…​.labels.jsonl` — the evaluator's answer key.** `ground_truth`, the discriminating
  probes, the expectation, and whether the run passed. A separate file with its own artifact
  kind, joined to the data by `trajectory_id`; `newest("training_export")` cannot resolve to
  it (the `recorded_world_truth` precedent, ADR 0040).
* **`…​.manifest.json` — what was exported.** Every `template_id` and scenario emitted, the
  splits present, digests of both files and of every source trace, and `absent_fields`: what
  plan 03 § 16.4 and plan 02 § 7 ask for that this export cannot honestly fill, with the
  reason, stated once instead of as a null on every line.

Three refusals, all hard errors that write nothing:

* A scenario whose split is `holdout`, **or** whose `template_id` any held-out scenario
  shares, is named and the whole export is refused.
* A scenario the corpus does not hold is refused: with no split, the export cannot show it is
  not held out, so it does not assume.
* `refuse_templates_seen_in_training(...)` is plan 03 § 4's third gate, for the report to
  call: a template any manifest names cannot be scored as if the policy had not seen it.

**No model prose travels at all** — not the candidate's free-form `name`, not the action's
`reason`, and not the short `reasoning` the hypothesis schema carries. Every value on a
trajectory line is a closed-enum label, an id, a number, a boolean, a tool name or a digest.
The one exception is an action's `arguments`, which the agent chose and which are the action.

The export is a pure function of (traces, corpus, timestamp, invocation id): the same traces
render the same bytes, and the data lines carry no clock at all, so only the manifest moves
when the same traces are exported again.

### Why the alternatives lose

Dump-and-delete-the-secrets fails in the direction that matters, and this repo has already
paid for that lesson once: `Scenario.agent_visible()` is an allow-list for exactly this
reason (ADR 0038). The next field added to a step record would be exported by default, and
the failure would be an omission — invisible in a diff and discovered, if ever, in a training
set. Two sub-decisions inside option 1 lose on their own terms. Inlining tool output makes
the export self-contained, which is genuinely convenient, and ships whatever a DLQ payload
said into the training loop; the ref plus digest keeps the content one lookup away in the
append-only store that already holds it, and the digest proves it is the same content.
Keeping `reasoning` is permitted for the *trace record* by plan 02 § 7, and the trace record
is read by a person looking at one run; this file is read by a training job, where unreviewed
model text is the thing a future reader is most tempted to feed back to a model. The trace
store keeps it, so nothing is lost and opening that door later takes its own ADR.

Filtering held-out templates instead of refusing is the option that looks kinder and is not:
it emits everything else, and the holdout promise then rests on somebody reading a warning
next to a successful export. Plan 03 § 4 says the export *refuses*, and a refusal that leaves
no file is the only version of it that cannot be missed.

### Consequences

Positive:

* The one artifact that leaves the harness cannot carry untrusted content, the answer key, or
  a held-out template, and each of those is a test rather than a sentence.
* A report can ask, in one call, whether a template was ever exported for training.
* An export is evidence: re-derivable byte for byte, and the traces it was built from are
  provably unchanged (the manifest digests them; the test checksums them either side).

Negative:

* The data is not self-contained — a consumer that wants the observation's content must read
  the trace store. Mitigated by the ref: file, record id and digest, all three on the line.
* A trajectory with no `reasoning` is a weaker SFT target than one with it. Accepted: reward
  v0 is deterministic (plan 03 § 16.1) and does not read prose, and adding a prose field later
  is an additive schema bump behind its own decision.
* `execution_mode` and `recorded_world_id` are absent, because the trace store's
  `scenario_start` record does not carry them; a recorded run and a live one are
  indistinguishable in it. The export says so in `absent_fields` rather than deriving a value
  that would label every recorded run wrongly. Adding those two fields to `scenario_start` is
  a follow-up order.
* `reward_components` is absent: WP-15.2 owns reward v0, and computing components here would
  be a second definition of the reward beside that one — F-011's shape.

Revisit trigger: a training run that cannot be built from this shape, or the day a template
is actually moved into `holdout` (nothing is held out today —
`TestNothingIsHeldOutWithoutADecision`).

## More information

* Plan 03 § 4 (splits), § 16.4 (the export), plan 02 § 7 (the step record), plan 06 D7.
* [ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md) (allow-list
  projection), [ADR 0040](0040-a-ground-truth-is-a-statement-about-one-world.md) (the truth
  sibling), [ADR 0021](0021-run-archives-are-locked-by-the-filesystem.md) (append-only on
  disk).
* CLAUDE.md invariants 4 and 9; `docs/eval-methodology.md` § The training export.
* Implemented by WO-R3-239 (WP-15.1): `evals/export.py`, `tests/unit/test_export.py`.
