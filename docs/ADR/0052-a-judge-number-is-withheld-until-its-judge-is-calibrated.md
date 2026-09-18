# ADR 0052: A judge number is withheld until its judge is calibrated, and stability is measured rather than set

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh

## Context and problem statement

`evals/graders/llm_judge.py` has scored every briefing in this campaign, and its module
docstring has said the same thing since Phase 2: regression gating on judge scores is deferred, "the
bar is set from baseline in Phase 3 once we have a real distribution." The distribution arrived. The
check on the instrument did not.

Two facts sit beside each other today and should not:

1. **The research report prints a judge number.** `arm_summary` has carried
   `judge_mean_overall` in the strategy leaderboard since WP-2.5, with nothing next to it saying
   what that mean is worth.
2. **The one judge number we have ever audited was wrong.** INC-002: the briefing judge read
   `list_dlq_messages -> {"total":0}` without the `remediation_hint='replay_safe'` that scoped it,
   concluded "all 5 messages are gone", and scored an honest briefing 0.0 for groundedness on a
   green paid archive. It misled nobody only because nothing gated on it.

The selector half of this problem was already settled: plan 02:243 is categorical — "no selector
number is reported before its calibration report exists" — and WP-6.2 built the gate, the register
(`research_report.CALIBRATION_REPORTS`) and the withholding. The register is empty, so every
selector number in the document is withheld and says so. Nothing equivalent existed for the judges,
and plan 04:169's acceptance for THIS packet is the same sentence: no number appears in a report
without a calibration report id beside it.

Then the protocol itself does not fit the repo. Plan 03 § 9.1 asks for **temperature 0** as the
first of five determinism settings. No judge call in this repo sends a temperature, and ADR 0048
plus owner decision O-24 (2026-09-17) say nothing built from here on may *require* one:
`llm/client.SAMPLING_REJECTED_MODELS` lists the model families that reject `temperature` with a
400, so a calibration pinned to temperature 0 would stop working on the first re-pin — which is
exactly the moment a calibration is most needed.

And two of the three judges plan 03 § 104 names are wrong about what exists. `plan_approval_judge`
does not exist in any form (divergence B6): every approve/refuse decision about a remediation plan
is deterministic guard code, by design, because invariant 4 forbids deriving a control from model
output. `action_verifier` — the only judge whose verdict gates a live OUTCOME — is omitted from that
list (divergence J6).

## Decision drivers

* **A number nobody has checked is an opinion in a table of measurements.** The report's own
  discipline elsewhere is to withhold and say why (`WITHHELD`, `_not_measurable`), never to print a
  softer number.
* **INC-003's rule:** a null must never read as a zero, and a label must never be applied to a
  question it was not written about.
* **O-24 and ADR 0048:** determinism may not depend on a sampling parameter.
* **Architecture principle: the structural fix.** A calibration that builds its own copy of a
  judge's context measures a judge nobody calls. The rule has to be enforced by the call path, not
  by care.
* **A calibration may not be certified by the measurement it releases.** Whatever the selector's
  ground-truth leg is, it cannot be `selected@k`.
* **Cost.** A real sweep is 5 × trap set × judges of billed calls. Under O-22 it is deferred, so the
  harness has to be provable for nothing.

## Considered options

1. Print judge numbers as today, and add a footnote when a calibration exists.
2. Gate judge numbers on a register, exactly as the selector's are gated (chosen).
3. Gate them on a calibration report found on disk.
4. Make judge scores gate runs as well (regression gating), now that a distribution exists.

For determinism, separately:

1. Send `temperature=0` as plan 03 § 9.1 says.
2. Make it configurable, defaulting to none.
3. Send none, and measure stability instead (chosen).

For the calibration set:

1. Calibrate the three roles 03 § 104 lists.
2. Calibrate the three that exist — `action_verifier`, `briefing_judge`, `candidate_selector` — and
   record the absent one as absent (chosen).

## Decision outcome

**Option 2, in four parts.**

### 1. A judge number is withheld until its judge has a calibration report id

`research_report.JUDGE_CALIBRATION_REPORTS` is a declared register keyed by the judge's normative
role name, sitting beside `CALIBRATION_REPORTS` and read through one function.
`arm_summary`'s `judge_mean_overall` is replaced by `WITHHELD_JUDGE` — a sentence, never `null` and
never `0.0` — unless `briefing_judge` has an id. The row also carries `judge`,
`judge_calibration_report_id` and `judge_gate`, so a reader can see which case it is in without
knowing the rule.

**`judged_runs` is not withheld.** How many runs carry a judge score is a coverage fact about the
arm, true whatever the judge turns out to be worth. This is the same split `_selector_number` makes
when it gates the selector fields and leaves `pass@k` alone: withholding the denominator as well
would stop a reader telling "not calibrated" from "never judged".

**Declared, not discovered (option 3 rejected).** Deriving the register from whatever file is on
disk would make the gate open itself — the argument is already written into
`CALIBRATION_REPORTS`'s own comment. Adding an id is the reviewable act of saying "this judge's
numbers have been checked, and here is the artefact." A fake-client report can never be that
artefact: `CalibrationReport.is_a_measurement` is false for one and its `judge_client` field says
`fake` on its face.

**This does not make anything gate on a judge score (option 4 rejected).** Judge scores stay
informational; no run passes or fails on one, and a red still means what it meant. What is gated is
a *report*. Turning judge scores into a regression gate changes what a red means and needs its own
decision with a distribution behind it — and the distribution is what the deferred sweep produces.

### 2. Stability is measured, not set

The calibration sends no temperature. What plan 03 § 9.1 wanted from temperature 0 — a judge that
does not wander — is bought two ways instead:

* **structurally**, by what is already there: forced tool use against a fixed JSON schema, a closed
  verdict space per role, and validators that reject an answer outside the set;
* **empirically**, by the self-agreement leg: the same input asked N=5 times, reporting the fraction
  of identical verdicts. Plan 03 § 109's own reading of it is the point — low stability means the
  rubric is ambiguous, high stability with low accuracy means the rubric is wrong.

A setting is a claim that a judge is stable. Five identical verdicts are a measurement that it is,
and the measurement survives a re-pin that the setting would 400 on. The report states
`determinism.temperature_sent: null` with the reason, so the absence is never read as an oversight.

### 3. Four legs, and a refusal is a result

`evals/judge_calibration/` holds a hand-built trap set of six cases per judge (plan 03 § 107 asks
for five; the sixth in each set is a regression — INC-002's briefing, the reported-effect verify,
the contradicted candidate). Each case **asserts** its verdict and carries the argument for it, so
the ground truth is the evaluator's and depends on no run.

Every case is put to the judge through the run's own call — `judge_verification`,
`judge_briefing`, `select_candidate` — which is why this packet makes two functions public
(`format_briefing_context`) and extracts one (`judge_verification` out of `transition_verify`). A
second copy of a judge's context inside `evals/` is INC-002's failure exactly: one rule about how
evidence may be read, given to one reader of it and not the other.

The third leg, ground-truth agreement over runs (plan 03 § 109), is measured for `action_verifier`
only, and read rather than re-run: every live archive already holds the verdict it gave, so the leg
costs nothing. The other two are refused, and the refusals are the interesting part:

* **`candidate_selector`: circular.** Its agreement with a scenario's labelled root cause *is*
  `selected@k` at k=1 — the term `oracle_gap@k` is built from, and the number a calibration report
  is what releases (plan 02:243). A calibration may not be certified by the measurement it
  certifies. Its trap set is its ground truth for that reason.
* **`briefing_judge`: no label exists.** Nothing in the system deterministically labels a briefing
  useful. The five graded dimensions are statements about the run, not about its prose, and a proxy
  built from them would score this judge against a different question. What it needs is human
  labels; INC-002 is one, by hand, and it is trap `bj-05`.

`action_verifier`'s leg is narrow on purpose. A row is paired only where the scenario declares a
Tier-1 action and expects to end `resolved`, because only there is `verified` unambiguously the
right verdict. Read-only scenarios (whose OUTCOME grade is a finding about the loop) and the
stabilize-only handover (where the action worked and the incident is still not over) are excluded
with their reasons attached — grading them would be INC-003's error in judge form. And the leg is
one-sided, which is the finding to carry out of it: **no live run in the committed archives was
ever supposed to end `not_verified`**, so the dangerous direction — a judge blessing a fix that had
not landed — is unmeasured by the archives and only the trap set reaches it.

### 4. The calibration set is the judges that exist

`action_verifier`, `briefing_judge`, `candidate_selector`, named by plan 02 § 3's normative
vocabulary, with `action_verifier` introduced as a constant beside the `verification_judge` prompt
file it has always loaded. `plan_approval_judge` gets a register entry
(`roles.ABSENT_ROLES`) saying it does not exist and why, printed in every report — because a reader
who finds no section for it cannot tell "not calibrated" from "not a judge", and plan 02:22 says
"Exists today? Yes", so the next reader will go looking.

The prompt file is **not** renamed. Renaming it would move a snapshot hash and change the
`_verify_judge` marker that 156 committed trajectories carry, to buy nothing.

### 5. A rubric edit lands one line at a time

Plan 03 § 110's rule is a review convention — a test cannot judge a commit — so it is written down
in `docs/eval-methodology.md` and here, and made **checkable after the fact**: every report carries
the sha256 and the line count of the exact prompt bytes it calibrated. A delta between two
calibrations is attributable only if each says which rubric it measured. The convention: one rubric
line per commit, with a rerun; a diff that moves two lines is split, and a reviewer who sees two is
asking for that split, not making an exception.

**No rubric line is edited in this packet.** A calibration measures the rubric as it stands, and
editing one in the same change as the instrument that measures it would leave the first number
un-attributable. So no judge-prompt snapshot hash moves and `make eval-reg` is unchanged.

### Why the alternatives lose

**Print the number with a footnote (1).** A footnote next to a float is read as a caveat about a
measurement. `judge_mean_overall: 0.86` with a note beside it will be quoted as 0.86; a sentence
where the number should be cannot be.

**Derive the register from disk (3).** The gate would open itself the first time anybody ran the
harness, including against the fake judge. The whole value of the gate is that a person had to
decide the calibration was good enough.

**Gate runs on judge scores (4).** A change to what a red means, on an instrument this packet is the
first check of. It also inverts the order: the bar comes from the distribution, and the
distribution comes from the deferred sweep.

**Send temperature 0 anyway.** Correct today on `claude-haiku-4-5`, a 400 on the first re-pin to a
newer family, and ADR 0048 already refused this for the selector on the same grounds. Repeating it
in the code whose job is to be trustworthy across model changes is worse.

**Calibrate the three roles the plan lists.** Two of them would be wrong: one does not exist, and
the judge with live consequences would be left out. A calibration that skips the judge whose verdict
resolves incidents is the wrong half.

## Consequences

### Positive

* Every judge number in the research report is withheld with a reason, today, and becomes readable
  the moment somebody accepts a calibration — the same shape the selector numbers already have.
* The `action_verifier`'s live track record is measurable for nothing: 19 paired rows out of 30
  scanned, 18 verdicts matching what the scenario called for. The one disagreement is
  `7acd2b441961 remediate_stale_cache_success`, already on record as a world-sensor limitation
  (WO-R3-267) rather than a judge error — which is exactly the confound the leg warns about, showing
  up where it was predicted.
* One spelling of each judge's question, shared by the run and its calibration. A future change to a
  judge's context reaches the calibration automatically instead of drifting away from it.
* The whole harness is provable at zero cost, and the paid leg is one command behind two flags.

### Negative

* **The research report's JSON changes shape.** `judge_mean_overall` can now be a string, and three
  keys are added to each leaderboard row. The Markdown half does not print this number, so the
  rendered document is unchanged; the next `--write` will carry the new JSON, beside the committed
  one and never over it.
* **A number that used to be there is now absent** until somebody does the work. That is the point,
  and it is still a cost: a reader of today's report learns less about the judge than yesterday's
  reader thought they did.
* **The trap set is the evaluator's opinion, written down.** Six cases per judge, each argued for in
  prose. A trap whose asserted verdict is wrong would make a correct judge look broken, so the
  argument beside each case is load-bearing and has to be reviewed like code.
* **`remediation.py` gains a public function.** `judge_verification` is now callable from outside
  the transition, which is a slightly wider surface than a closure.

### Neutral

* No prompt bytes change, so no snapshot hash moves and `make eval-reg` stays at its current corpus
  with no changes against the blessed baseline.
* Judge scores remain informational. Nothing gates on them and no run's verdict moves.
* `evidence/sync.sh` needs no new line: since WO-R3-257 it syncs `evals/reports/` in one recursive
  pass, and `judge-calibration/` is inside it.

## What this deliberately does not do

* **No paid sweep.** Owner instruction O-22. The command, its estimate and its acceptance go to
  `.coordination/DEFERRED-PAID-RUNS.md`; the register stays empty until it runs.
* **No plan-approval judge.** Building one is a change to invariant 4, not a calibration packet.
* **No rubric edits**, for the attribution reason in § 5.
* **No re-judging of committed briefings.** The briefing judge's ground-truth leg needs human
  labels, not another model's opinion; asking a judge to grade a judge would move the question
  rather than answer it.
* **No phase-close wiring.** `evals/phase_close_report.py`'s "judge calibration" section still reads
  "not rerun", correctly — it has no report to read yet. Wiring it to this register belongs with the
  packet that runs the sweep.

## Revisit trigger

* The first real calibration. If trap accuracy is high and stability low, the rubrics need clearer
  checks; if stability is high and accuracy low, a rubric is checking for the wrong thing — and
  either finding gets an `INCIDENTS.md` row before any fix (07:49).
* A second judge column in the leaderboard, which would need its own register key rather than
  inheriting `LEADERBOARD_JUDGE`.
* Human labels for briefing usefulness arriving, which would turn that refusal into a measurement.
* A live run that is supposed to end `not_verified`, which is what would let the archives measure
  the false-approve direction.

## More information

* Plan 03 § 9 (the protocol), plan 03 § 107 (the trap shapes), plan 03 § 110 (attribution), plan
  04:167-169 (WP-6.3 and its acceptance), plan 02 § 3 (the naming), plan 02:243 (the gate).
* Divergences: **B6** (`plan_approval_judge` does not exist), **J6** (`action_verifier` belongs in
  the set), **D2** (a new artifact family needs a `KINDS` entry), and 03:112's flat report path,
  which cannot carry "one per judge".
* INC-002 (`audit-ws/context/INCIDENTS.md`) — the judge error this exists to catch.
  INC-003 (same ledger) — a label applied to a question it was not
  written about, which is why the track record's pairing is narrow.
* Related: [ADR 0048](0048-a-selector-is-constrained-by-its-schema-not-by-a-temperature.md) (no
  required sampling parameter; the argument reused here),
  [ADR 0049](0049-the-oracle-gap-is-an-evaluator-number-paired-within-one-world.md) (the selector
  gate this one mirrors), [ADR 0035](0035-a-parse-failure-of-our-own-output-is-a-harness-event.md)
  and its WO-R2-174 amendment (the bounded repair that makes a judge calibratable at all),
  [ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md) and
  [ADR 0040](0040-a-ground-truth-is-a-statement-about-one-world.md) (whose ground truth, and about
  which world).
* Work order: WO-R3-210 (WP-6.3). WO-R2-174 was already complete before this packet opened.
