# ADR 0049: Keep the oracle gap on the evaluator's side, and pair it within one world

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh

## Context and problem statement

WP-6.2 adds the `candidate_selector` arm and the number the whole research buildout is for:
`oracle_gap@k = pass@k − selected@k`. A small gap says generation limits the agent; a large one
says selection does. It is the headline finding, which is exactly why it is the number most worth
getting wrong.

Three things about it need deciding, and each has a wrong answer that looks reasonable.

**Whose number is it?** Both terms need the scenario's `ground_truth`: pass@k asks whether a
correct cause was in the candidate set, selected@k asks whether the selector took it. Ground
truth is evaluator-only (ADR 0038, ADR 0040). So the gap cannot be computed anywhere the agent
can read, and the selector cannot be told anything that would let it compute its own.

**What does it pair on?** A difference between two numbers measured in two different worlds is
not a difference between two capabilities. This repo has already paid for that lesson: INC-003
applied labels written about the canned world to a live unseeded one and produced a live
root-cause figure of "61%" that had to be withdrawn. The research report currently pairs
instances by scenario NAME and says so in its own limits.

**What does the emitted step carry on a `select`?** `InvestigationStep` re-sorts its
`hypotheses` by confidence at the schema boundary (B-07), and three gates read index 0 as the top
pick — the remediate gate, the ADR-0009 re-probe prior, and the remediation planner's target. So
emitting the whole candidate set with the selection first does not make the selection the top
pick; a more-confident *unselected* candidate sorts above it.

## Decision drivers

* The trust boundary is the benchmark's whole claim (ADR 0038). A selector that can see the
  answer key is not being measured on selection.
* Plan 03 § 12 / 03:145: compare arms on the same instances. A difference computed across
  instances carries a number nobody should read.
* Plan 02:243 and plan 04:169: no selector number is reported before its calibration report
  exists.
* "Selection is not authorization" (plan 02 § 18). The gates decide, whatever the selector chose
  — so the gates must be applied to what the selector actually chose.
* `docs/architecture-principles.md`: prefer the structural fix. A property enforced by a refusal
  beats a property described in a limits section.

## Considered options

For pairing:

1. Pair by scenario name, as the report does today, and footnote the risk.
2. Pair by a world key, with a rule per execution mode, and refuse a cross-world difference
   (chosen).

For the emitted step on `select`:

1. Emit the whole set with the selection first, and accept the re-sort.
2. Raise the selected candidate's confidence so it sorts first.
3. Emit only the selected candidate's hypothesis (chosen).

## Decision outcome

**The gap is the evaluator's, structurally.** It is computed in `evals/candidate_metrics.py` and
`evals/research_report.py`, neither of which is importable from `src/incident_commander`. Both
terms need `ground_truth`, which is not on `AgentVisibleScenario` and is not a parameter of
`agent/selection.format_selection_context` — so there is no argument a label could arrive
through, and a sweep over every root-cause-graded scenario asserts none reaches the selector's
rendered context. The selector reads the alert, the run's own evidence ledger and the candidate
set the model itself produced. Nothing else.

**The gap is paired within one world, and `WorldKey` is that world.** One rule per execution
mode, declared in one mapping:

* `canned` — the fixtures ARE the world and they are committed, so two canned runs of one
  scenario are one world and pair.
* `live` — a live world is only ever itself, so the key is the ARCHIVE and two live runs of one
  scenario never pair. Nothing guarantees they met the same database, the same queue depth or the
  same seeded fault.
* `recorded` — a recording IS a world (ADR 0043), so the recording's own identity is the key and
  two arms replayed against one recording do pair. That identity is
  `evals/recorder.world_fingerprint`, which is already this repo's answer to "are these two
  recordings of the same world": same scenario, same label, same calls with the same wired
  arguments and the same answers, minus the volatile fields and the recording session's own
  provenance. Not the file path (a path is a name) and not the archive (an archive is a run). The
  runner puts it on every recorded outcome as `replay["world_fingerprint"]` (WO-R3-198, ADR 0047),
  so nothing new is minted for this decision — it reads what recorded mode already writes. That is
  the mode plan 03 § 5 puts strategy comparisons in, and it is the mode this key exists for.

A mode with no rule is refused rather than given another mode's. Asking for a gap across two
different keys raises `OracleGapAcrossWorlds`, naming both. In the ordinary case both terms come
off one run's own step records and are paired by construction — there is no code path on which
they come from different runs.

**Every selector number is withheld until its arm has a calibration report.**
`research_report.CALIBRATION_REPORTS` is a declared register keyed by the arm
(`strategy/generator/n`), empty today because WP-6.3 has not run. A row without an id has its
selected@k, oracle gap, uncertainty and decision replaced by a sentence saying so — not by
`null`, which the next line of almost any reader takes for zero. The generation half (pass@k, the
duplicate rates) is NOT gated: it measures the generator and has nothing to do with the
selector's calibration.

**On `select`, the emitted step carries the selected candidate's hypothesis and only it.** The
alternatives stay in the `StepRecord`'s `candidate_set`, where all N of them are, and where every
pass@k is computed from.

### Why the alternatives lose

**Pairing by scenario name.** It is what the report does today and it is honest about it, but the
oracle gap is the first metric where the pairing is the whole claim rather than a caveat — the
number *is* a difference. A footnote is the shape INC-003 came in: the report said which archives
were in scope and the reader still quoted the figure. A refusal cannot be quoted past.

**Emitting the whole set with the selection first.** This is the reading that looks obvious and
it silently breaks the arm: the schema re-sorts, so on any step where an unselected candidate is
more confident, the loop gates on a diagnosis the selector rejected. The run would then act on
one diagnosis while the record said the selector chose another, and every selected@k computed
from that record would be measuring the wrong thing. The cost of the chosen option is real and
named below.

**Raising the selected candidate's confidence to make it sort first.** It works and it fabricates
a number. `confidence` is the model's own statement about a candidate; overwriting it to move a
sort order would put a value in the record that nothing said.

### Consequences

Positive:

* No selector number can be reported across two worlds, and none can be reported before its
  calibration report exists. Both are refusals in code rather than sentences in a limits section.
* `selected@k ⟹ pass@k` holds by construction — both are scored at the same step against the same
  truncation — so the gap is in `{0, 1}` per run and a negative one raises rather than printing.
* The gap becomes measurable the moment a selector archive enters the report's scope, with no
  further packet editing the report.
* Committing to nothing (`probe_more`, `escalate`) is scored as a MISS for selected@k, not as an
  abstention, so the selector cannot look best exactly when it decides least.

Negative:

* **The run's hypothesis ranking on this arm is one hypothesis.** A briefing written after a
  selected step sees the commitment and not the alternatives, so the LLM judge's soft dimensions
  are not comparable between this arm and `baseline` — the same limit ADR 0044 records for the
  enumerated arm's derived `reasoning`, and for the same underlying reason. Mitigation: the whole
  candidate set is on the `StepRecord`, so nothing is lost to the research data; the limit is on
  the briefing and is stated in the arm's own module docstring and in the PR.
* **Two live runs never pair**, so an oracle gap over live archives is refused even when the two
  runs did happen to meet the same world. Mitigation: it is refused in the direction that is safe,
  and recorded mode — which landed in WO-R3-197 / WO-R3-198 (ADR 0046, ADR 0047) while this packet
  was being built — is the mode the comparison is meant to run in. Its `world_fingerprint` is what
  the `recorded` rule keys on, so the pairing case is reachable today rather than reserved.
* **The calibration register is hand-maintained**, so a real calibration report that nobody
  declares leaves the gate shut. Mitigation: that is the failure direction worth having, and the
  register is a constant in a reviewed file rather than a directory scan that would open itself.
* A `probe_more` whose chosen candidate proposes no probe ends the run. Mitigation: it stops
  through the existing terminal path with the reason named, rather than being replaced by the
  generator's own step (overruling a decision the model made) or raising (a crash for a model's
  incoherence).

Revisit trigger: the first paired comparison actually run in recorded mode. `recorded` is the
ordinary key now that the mode exists, so the question that reopens this record is the `live`
rule's cost — no pairing at all — which should be re-read once there is a recorded comparison to
contrast it with: a live run whose fault the runner seeded and whose world audit passed may be a
world worth keying by a recording of it rather than by its archive.

## More information

* Plan `02_INCIDENT_COMMANDER_PLAN.md` § 12 (02:241, 02:243), `03_EVAL_RESEARCH_PLAN.md` § 7.3,
  § 9, § 12, `04_IMPLEMENTATION_WORKPLAN.md` WP-6.2 (04:164) and WP-6.3 (04:169).
* [ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md) — the projection
  that keeps the answer key off the agent's side.
* [ADR 0040](0040-a-ground-truth-is-a-statement-about-one-world.md) — a ground truth is a
  statement about one world, which is what the world key makes operational for a difference.
* [ADR 0043](0043-a-recording-is-keyed-by-what-the-agent-sends.md) — why a recording is a world.
* [ADR 0046](0046-a-replay-answers-the-call-that-was-made-at-the-clock-it-is-replayed-at.md) and
  [ADR 0047](0047-a-recorded-run-grades-diagnosis-and-the-plan-and-nothing-else.md) — recorded
  mode itself, and the `world_fingerprint` the `recorded` rule keys on.
* [ADR 0044](0044-evidence-ids-are-rendered-for-the-arms-whose-schema-cites-them.md) — the
  matching limit on the enumerated arm's `reasoning`.
* [ADR 0045](0045-a-sampled-step-is-one-samples-and-every-draw-is-charged.md) — the accrual trap
  this arm hits one layer up, and the shape of the fix.
* [ADR 0048](0048-a-selector-is-constrained-by-its-schema-not-by-a-temperature.md) — the
  selector's own output contract, including what `probe_more` points at.
* INC-003 (`audit-ws/context/INCIDENTS.md`) — what applying one world's expectations to another
  cost.
* Implemented by WO-R3-209.
