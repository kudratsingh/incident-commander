# ADR 0079: A sampled study pins every world, and one paid invocation runs one batch

* Status: accepted
* Date: 2026-10-10
* Decider: WO-R3-347

## Context and problem statement

The oracle-gap study (WO-R3-347, owner decisions O-37 and O-40) replays 13 recorded worlds four
times each under the `candidate_selector` arm. Three things in today's harness make that study
either unsound or unaffordable in owner time:

* `--world` pins **one** recording, and ADR 0047 refuses it beside a wider selection. Pinning
  13 worlds therefore takes 13 runner invocations per pass, 52 for the study.
* Owner decision O-45 asks for one explicit yes per paid invocation, retries included. 52
  invocations would be 52 asks for one study.
* Without `--world`, each scenario replays its **newest** recording. That is a choice nobody
  made: a re-recording landing mid-study would silently change the world under half the runs.

The report side has a matching gap: `research_report` reads only archives pinned in `SCOPE`,
has no pooled oracle gap, no interval on it, and scores pass@k against the corpus label even
where the recording's own answer key says the label does not describe that world (ADR 0040).

## Decision drivers

* ADR 0049: the gap is paired within one world, and a recording's identity is that world.
* O-45: one owner yes per paid invocation, so a batch must be one invocation.
* ADR 0047: one world is one scenario's world; nothing may be ambiguous about which recording
  a number came from.
* Plan 02:243: no selector number before its arm has a calibration report.

## Considered options

1. A driver that calls the runner once per world (13 archives per pass, one make command).
2. `--world` given once per selected scenario, all-or-nothing, so one invocation replays a
   whole batch, every world pinned; plus a study module that runs it and reports it (chosen).

## Decision outcome

**`--world` may be repeated** (or comma-separated). With one value, ADR 0047's rules are
unchanged. With several, `runner.pinned_recordings_for` requires each value to name exactly
one recording of one selected scenario, no scenario pinned twice, and **every selected scenario
pinned** — a batch that pins some worlds and lets others float to their newest is refused,
because those runs would come from worlds nobody chose.

**A study runs from a committed sample plan** (`evals/samples/oracle_gap.json`): the worlds and
their recording ids, the arm, the model role, and the budget multipliers. `make
oracle-gap-batch` checks the plan and prints the exact command for free; `YES_SPEND=1` (exactly
`1`) runs it as one runner invocation, i.e. one owner yes per batch. A placeholder id, an
unknown recording, or a recording whose answer key does not apply to its world is refused
before anything runs.

**The report scores only comparable runs and lists every other one with its reason**: a harness
crash, a replay miss (ADR 0047: not comparable), a not-graded label (ADR 0040), another arm, a
recording other than the one pinned, or no step records. Selector numbers are withheld until
`research_report.CALIBRATION_REPORTS` holds an id for the arm **and that report measured the
model the selector ran on**: the calibration harness asks `JUDGE_MODEL`, while a run's selector
is called on the agent's model, so a register entry alone could vouch for a different model (the
2026-10-08 selector report `4fc2722d286e` is on claude-haiku-4-5; the benchmark agent model is
claude-sonnet-4-6). The pooled gap's interval **resamples worlds, not runs**, because four runs
of one world are not four worlds.

### Why the alternatives lose

**One invocation per world.** It needs no runner change, but it makes 13 archives per pass and
leaves the owner's yes covering a loop the runner never sees as one thing. A refusal in world 7
would leave six worlds run and spent and seven not, a partial batch nobody asked for.

### Consequences

Positive:

* A batch is one archive, one report.json and one owner yes; every number in it names its
  recording.
* Unpinned replays cannot sneak into a pinned batch.

Negative:

* A recorded-mode make target now exists, which the runbook had declined ("a second place for
  the mode's refusals"). Mitigation: the target restates nothing; the runner's own refusals run
  unchanged inside it, and the runbook says so.
* `--only` still matches by substring in recorded mode, so a batch may select more than it
  pins. Mitigation: the all-or-nothing rule refuses that loudly before any spend.

Revisit trigger: a second sampled study (Phases 5, 9, 12, 13). If it needs another arm, the plan
file grows an arm list rather than a second module.

## More information

* ADR 0040, ADR 0043, ADR 0047, ADR 0049; plan 03 § 7.3, § 10, § 12.
* The design the owner approves: the hub's `docs/plans/research-buildout-v2.1/samples/oracle-gap.md`.
* Owner decisions O-37, O-40, O-43, O-45 (`audit-ws/.coordination/DECISIONS.md`).

## Amendment 2026-10-10 — a pilot, repeats in one invocation, and the calibration id beside every selector number (WO-R3-375, owner O-55)

*Status: accepted. Extends this ADR; changes one of its stated consequences (below).*

The owner approved the oracle-gap design on four conditions this ADR already met and two it did not
(O-55): a **pilot of 4 worlds × 3 seeds** before the full sample, and **the calibration report id
beside every selector number**, not once at the top. A "seed" here is a repeat run of the same
recorded world (the hub design § 2). Meeting both without breaking O-45 (one owner yes per paid
invocation) took four decisions.

**1. `--repeats N`: one runner invocation makes N passes, and each pass is its own archive.**
The runner gains `--repeats N` (recorded mode only; a live pass would act on the shared world
again with no reset between, ADR 0020, and a canned pass replays the same file to the same canned
planner). Pass 0 keeps the invocation's id; each further pass mints its own id and writes its own
archive, report, trajectories and briefings. So an archive still holds **one row per scenario**,
which every reader that joins a run by `(archive, scenario)` relies on (the research report, the
re-grader, the judge's track record, the label packet), and runs stay separated by identity as
invariant 9 asks. Each row's provenance says which pass made it (`RunProvenance.repeat`, from 0)
and which invocation it belongs to (`repeat_of`, pass 0's id, on every pass). Rejected: N rows of
one scenario in one archive, which would have needed a new file-naming scheme and a change to
every one of those readers, each a place a later repeat could be read as the first.

**2. `--study NAME`: every row says which sample plan asked for it.** A plan gains
`pooled_with_main` (default true). The pilot plan, `evals/samples/oracle_gap_pilot.json`, sets it
false: its report scores only runs stamped with its own study and says it is reported on its own,
and the main plan's report leaves every pilot run out with the reason. The stamp is needed because
the pilot and the main sample may pin the *same* recordings, so nothing else in a run tells them
apart. A run with no stamp counts for the main sample only.

**3. A recorded run that pins its worlds selects by full scenario name.** The pilot pins
`jobs_not_progressing_outbox_stall` without its `_deploy_noise` sibling. By substring that name
selected both, and the all-or-nothing pinning rule above then refused the whole batch — at paid
time, after the free check had passed. When `--world` is given in recorded mode, `--only` is now
matched by full name, as on the live path. This replaces the negative consequence recorded above
("`--only` still matches by substring in recorded mode, so a batch may select more than it pins"):
a pinned selection can no longer be wider than its pins. Unpinned recorded runs, canned runs and
`--smoke` keep substring matching.

**4. One renderer puts the calibration id beside every selector number.**
`research_report.selector_number(label, value, report_ids=…, why_withheld=…)` writes
`selected@8 = 9/12 [cal 4fc2722d286e]`, or, with no report that opens the gate,
`selected@8 withheld [<the reason>]`. The oracle-gap report uses it for every selected@k, gap,
interval, decision-split, selector-vs-top-candidate, per-run and agreement cell, and the research
report's oracle-gap section uses the same function. In JSON every such row carries
`calibration_report_id` (and, when withheld, `withheld_because`) in the row itself.

The report also lists each run with its repeat index and says, per world, whether its repeats
agreed on pass@k and on selected@k. `make oracle-gap-batch PLAN=<plan> REPEATS=N` is the one
command; the free check prints the runner command with `--repeats N` and the total run count.

Nothing here was run against a model; the pilot itself (12 recorded runs, ≈ $3.60) is a separate
paid ask.
