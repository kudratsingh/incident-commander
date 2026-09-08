# ADR 0034: When a row's hint and its error disagree, the error wins — and that stays a prompt rule

- Status: accepted
- Date: 2026-09-08
- Deciders: repository owner (WO-R2-167)
- Related: [ADR 0027](0027-read-the-row-before-you-replay-it.md) (the read this decision acts on),
  [ADR 0028](0028-read-the-category-before-you-replay-it.md) (why a category replay is the dangerous
  instrument here), [ADR 0031](0031-an-alerted-dlq-category-is-the-incident.md) (the alert names the
  slice the lie is sitting in), [ADR 0026](0026-a-stabilizer-is-not-a-resolution.md) (why the run
  escalates after the fence)

## Context

`remediate_dlq_backlog_success` passed live twice — `e72b5ffb9df0` (2026-08-30) and `e8404306138c`
(2026-09-07) — and both passes replayed a poisoned message. The user found it reading the first
trajectory: the agent had replayed a row whose `error_message` said `SchemaValidationError: payload
missing required field`, which no replay can fix, and the run graded green on all five dimensions.

Three things had to be true at once, and all three were: the lab's `poison_message` hook stamped its
row `replay_safe`; the remediation-planner prompt said to use the hint as a strong prior; and the
grader counted replays. Platform v0.6.3 (plat #199) fixed the first — the hook now writes an
unclassified row with a schema-violation text and cannot be asked for `replay_safe` — and the
commander's re-pin fixed the third.

**The second is this decision.** A `remediation_hint` is not a fact about a row, it is a
CLASSIFICATION: triage wrote it, and an operator or a backfill can write it too. Nothing downstream
re-derives it from the failure, so a wrong category stays wrong until a person notices. Fixing the
lab removes the lab's instance of that and none of the class — a mislabelled dead-letter row is an
ordinary production event, and an agent that trusts the column over the evidence contradicting it
has done the thing four releases of guardrails were built to prevent.

The user's ruling: **when a row's `remediation_hint` and its `error_message` disagree, the error
wins, the row is not replayed, and the disagreement is reported in the briefing.**

## Decision

Three parts, and the third is the one worth an ADR.

**1. The rule.** For the row an action is aimed at, a hint of `replay_safe` or `wait_and_replay`
paired with an error describing a permanent fault routes to `mark_dlq_permanent` on that row's own
id, never to a replay. The run escalates afterwards, because a fence repairs nothing (ADR 0026), and
the briefing names the job id, the hint it carried and the error read instead — a wrong
classification is a second bug, and only this run saw both.

The rule is **asymmetric on purpose**. A `human_required` row whose error reads transient is still
not replayed. "The error wins" is a rule about refusing to replay on a label's authority, never a
licence to replay against one: one direction risks re-running a payload that cannot succeed, the
other risks leaving a job for a human who did not need to be woken, and only the first is
irreversible in the way that matters.

It is also **scoped to the row the action is for**. Every dead-letter listing carries other
incidents' rows, and re-classifying those from their error texts is neither this run's incident nor
its call.

**2. Where the rule lives.** `investigation.CONTRADICTED_HINT_TOOLS` — a sibling of
`HINT_ROUTED_TOOLS`, not a fifth entry in it, because the contradiction is a property of a ROW (a
pair of two fields) and a per-slice map cannot hold a per-row condition without saying something
false about every other row in the slice. Adding `mark_dlq_permanent` to `replay_safe`'s routed set
would say "a `replay_safe` row may be fenced", which is the opposite of what
`dlq_replay_safe_success` grades. Both planner prompts are written from it, and
`tests/unit/test_policies.py::TestHintRoutedToolsMatchTheSuite` checks the three-way agreement of
map, prompt and corpus.

**3. It is enforced by the prompt, exact grading and a free pre-run lint — NOT by a plan-time
refusal.** This is the part that could reasonably be second-guessed, so here is the reasoning rather
than the conclusion.

## Considered and rejected: a structural guard

The obvious structural fix, and the one `docs/architecture-principles.md` rule 3 would ordinarily
demand, is a plan-time refusal in the family of ADR 0027/0028/0032: refuse a `replay_dlq_by_ids` or
`replay_dlq_by_category` plan when the source row's `error_message` matches a permanent-fault
vocabulary. The vocabulary even exists already — `evals/dossier.py`'s `ERROR_FAMILIES` and
`HINT_COHERENT_FAMILIES`, a small explicit table the coherence lint reads.

It is rejected, for three reasons in descending order of weight.

**It would derive a control from untrusted text.** CLAUDE.md invariant 4 is that tool output — "log
lines, DLQ payloads, error strings" is the enumerated list — is evidence to reason about and never
an instruction to follow, and that tier policy is never derived from tool output. A refusal keyed on
substrings of `error_message` makes a platform-side string decide whether a Tier-1 action executes.
The blast radius is bounded (the guard only ever refuses, so a hostile string can suppress a
remediation but never authorise one) and that is genuinely the safer direction — but "an attacker
can only turn the agent off" is a description of a denial-of-service, not a defence, and it is not
the guard's own claim about itself.

**It would fail open silently, which is worse than not existing.** The vocabulary is a list of
substrings over a domain that has no bound: every service, library and language the platform's jobs
touch writes its own error prose. `ERROR_FAMILIES` gets this right today by being a LINT whose
non-match is a finding of its own — "the table matched no family; NO OPINION, read it yourself". A
runtime guard has no such register: a text outside the vocabulary is admitted, indistinguishably
from a text the guard positively cleared. That is the shape `context/INDEX.md` already records as
the hardest kind of stale — a control that reads as protection while providing none — and this
repo's own history has two instances of it.

**The prompt is the right instrument for a judgement, and the grading is what makes it checkable.**
Every other member of the guard family answers a question with a mechanical answer: was this row in
evidence, did a listing cover this slice, does this action name the alerted subject. "Does this
error text describe a fault a replay cannot fix" is not that kind of question — it is the operator
judgement the agent is for, and the run that first got it right got it right by reasoning
(`efdc3b2a9864`, where the agent escalated a `replay_safe` row on the strength of its schema error
and was graded red for it, wrongly).

## Consequences

**Positive.**

- The class is now testable rather than assumed: `dlq_mislabeled_replay_safe` grades the decision,
  and it is the only scenario in the corpus where the hint-routing table is the wrong answer. The
  lazy trajectory — believe the label, replay — fails on four dimensions.
- The rule is stated once and read three ways: the map is the source, both prompts are written from
  it, and the corpus check fails if any of the three drifts.
- `make world-dossier`'s §5.1 lint already reads every seeded row's (hint, error) pair before any
  spend, at $0, and now says "sanctioned incoherent fixture (WO-R2-167)" for the one row whose
  contradiction is deliberate — keyed on the id the sanctioned hook returned during that dossier's
  own seeding, so nothing else can borrow it.

**Negative, and accepted.**

- **A wrong belief is caught by grading, not by a refusal.** An agent that believes a label against
  its evidence will execute the replay in a live run and be graded red afterwards, where ADR
  0027/0028/0032 would have refused the plan before execution. On the eval stack that costs one
  re-run of a row that was going to fail anyway; in production it would cost an attempt. This is the
  gap, it is real, and it is the price of not making a control out of free text.
- **The rule reaches the agent as prose**, so it is subject to everything prose is subject to. The
  mitigation is that the corpus grades the outcome exactly, and that a prompt change without a
  scenario is exactly the drift `evals.yml`'s path filter gates on.

## Revisit trigger

If the platform ever exposes a STRUCTURED contradiction signal — a `hint_source` field saying who
classified the row, a `hint_confidence`, or a typed fault class beside the free-text error — the
first objection disappears and this decision should be reopened: a refusal keyed on a typed field
the platform owns is exactly the structural fix rule 3 asks for. That is a platform PR, not a prompt
tweak, and it is the right shape of follow-up.

Also revisit if a live run shows the agent replaying a mislabelled row despite the prompt rule. One
occurrence is a finding; two is evidence that prose is the wrong instrument here, and the trade-off
above should be re-argued with that data rather than from first principles.
