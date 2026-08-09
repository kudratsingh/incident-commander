# ADR 0011: Freeze the eval for the fix campaign — suspend invariant 8, ledger the debt

* Status: accepted (time-boxed: in force for the fix-campaign window only; expires by its
  own sunset clause the moment the post-campaign eval restarts)
* Date: 2026-08-08
* Decider: Kudrat Singh

## Context and problem statement

The audit fix campaign repairs the eval harness and several behavior surfaces at the same
time, while the commander stays pinned to platform v0.4.9 and the fixes target the next
platform version. Maintainer decision, 2026-08-08: the eval is shut down until every
campaign fix is merged. Then the platform cuts v0.5.0, the commander re-pins to it by
digest, and only then does the eval run — and that run becomes the new baseline.

CLAUDE.md invariant 8 ("Evals gate behavior changes", CLAUDE.md:24) requires every PR that
touches prompts, tool definitions, policy tiers, memory retrieval, or the pinned model to
pass the regression eval suite before merge. That cannot be satisfied honestly while no
eval may run. The question is how behavior-surface PRs merge during the window without
either faking the gate or quietly dropping the rule. Definition of Done item 5
(CLAUDE.md:294) answers who must decide: "No invariant from this file is weakened without
an ADR that says so explicitly." This is that ADR.

## Decision drivers

* The maintainer's ordering is fixed: all fixes merged, then version cut, then re-pin,
  then eval — mid-campaign runs would measure a half-fixed agent with an instrument that
  is itself mid-repair, against a stale pin.
* The freeze is about *running* the harness, not about *touching* it. Roughly half the
  campaign edits eval source (`runner.py`, `regression.py`, `tracing.py`, `guards.py`,
  graders, scenario YAMLs, fixtures); conflating the two activities would skip that work
  for no reason.
* A canned green presented as invariant-8 satisfaction is the exact pathology the audit
  flagged (32/37 scenarios silently degraded). Whatever replaces the gate must not let a
  canned CI run impersonate eval evidence.
* A future reader must be able to tell a dated decision-with-expiry from an oversight.
  The invariant's text must survive the window intact, with one explicit record of its
  suspension (Definition of Done item 5, CLAUDE.md:294).
* The post-campaign run is only meaningful if every un-gated behavior change merged during
  the window is enumerated with a falsifiable acceptance check it can be walked against.

## Considered options

1. Keep invariant 8 in force: regression-eval evidence per behavior-surface PR.
2. Declare the canned CI job's green sufficient: invariant 8 "satisfied" by `make eval-reg`
   on canned fixtures.
3. Delete invariant 8 from CLAUDE.md for the window and restore the text afterwards.
4. Suspend invariant 8 explicitly under this ADR, with a per-PR replacement gate, an
   eval-debt ledger, and the post-campaign run as batch acceptance (chosen).

## Decision outcome

Option 4. Invariant 8 is **suspended, not deleted**, for the duration of the campaign
window. Its text in CLAUDE.md stays exactly as written (CLAUDE.md:24), as do its
restatements — Definition of Done item 3 (CLAUDE.md:292) and the working rule "Run
`make eval-reg` before declaring any prompt or policy change complete" (CLAUDE.md:304).
Those lines read as in force; for the window, this ADR is the overriding record that they
are suspended. Nothing in CLAUDE.md is edited: the ADR is the suspension record, exactly
as Definition of Done item 5 (CLAUDE.md:294) requires.

### What the freeze forbids, expects, and requires

| | |
|---|---|
| **Forbidden** | *Executing* the eval runner in any mode, from any checkout: `make eval`, `make eval-reg`, `make eval-live`, `make eval-smoke`, `make baseline`, `make demo`, `make eval-reset`, `make chaos-*`, `uv run python -m evals.runner` (any flags), any `EVAL_TRACE_DIR` run, any throwaway-worktree run, any baseline reblessing, any live campaign. |
| **Expected** | Reading, editing, and refactoring everything under `evals/` — `runner.py`, `regression.py`, `tracing.py`, `guards.py`, `graders/`, scenario YAMLs, canned fixtures — and prompts under `src/incident_commander/llm/prompts/`. These are ordinary source files. |
| **Required** | `pytest tests/unit` on every PR. `tests/unit/test_runner.py`, `test_regression.py`, and `test_tracing.py` are pytest unit tests *of* eval code, not eval runs. The distinction is mechanical: pytest imports and calls harness functions in-process; the freeze bans invoking `evals.runner` as a program. |
| **Unchanged** | `evals/{runs,reports,trajectories,briefings}/` and `study/` stay append-only (invariant 9) — now trivially, since nothing executes. The correct number of new files there this campaign is **zero**; any diff touching them is a review-blocking defect. `evals/reports/baseline.json` stays byte-identical until the post-campaign bless. |

### Replacement gate for invariant-8-surface PRs

The surface list is the mechanical test. It restates invariant 8's own list and extends it
with two entries this policy makes explicit: prompts under
`src/incident_commander/llm/prompts/`, tool definitions/registry, policy tiers, memory
retrieval, pinned model config in `config.py`, `contracts/platform-tools.snapshot.json`,
and `evals/scenarios/**` expectation changes. Any PR whose diff touches one of these must,
before merge:

1. Have CI's canned eval-regression job green — **necessary, never sufficient**. A green
   canned run is never to be described as "passed the regression eval suite".
2. Carry the mandatory eval-impact line in the PR description: `EVAL FROZEN per ADR 0011 —
   not run; debt row N`, plus one sentence naming the behavior that changed and the
   falsifiable observable the post-campaign run will check.
3. Append one row to [`docs/eval-debt.md`](../eval-debt.md) in the same PR.

### Compensating controls

In place of the suspended gate, behavior-surface changes are held to:

* unit and wire-shape tests for every changed surface;
* prompt-hash snapshot regeneration in the same PR as any prompt change;
* platform-SOURCE citations (file:line) for contract-adjacent changes, with the semantic
  confirmed at both the pinned v0.4.9 and platform master tip;
* the description-delta ledger for tool-description changes, compiled into the expected
  re-pin snapshot diff;
* the Phase 7 live run as **batch acceptance for all behavior-surface changes merged
  during the freeze** — that is the run the eval-debt ledger is walked against.

### CI: the canned `evals.yml` job stays enabled, exactly as-is

The `eval-regression` job in `.github/workflows/evals.yml` keeps running on its path
filter, unchanged. It runs `make eval-reg` offline/canned in the ephemeral CI checkout: no
API key, no platform contact, no spend, and its writes land only in that checkout. It is
an integration test *of* the harness, not an eval run — and while `runner.py` is being
heavily edited it is the only automated detector of gross harness breakage, which is
precisely why it stays.

It was verified live that `evals.yml` has never been a required status check on `main`;
**no branch-protection or repo-setting change is made or needed**. An earlier draft of
this decision called for suspending the check in repo settings; that draft is explicitly
reversed. What is suspended is invariant 8's requirement for regression-eval *evidence*
per PR — not this job. (The job's path filter is known to miss the real prompts directory;
the campaign fixes the filter while the gate is suspended so the restart inherits a
correct gate.)

### The eval-debt ledger

[`docs/eval-debt.md`](../eval-debt.md), created alongside this ADR, is append-only by
convention: one row per invariant-8-surface PR, appended in that same PR, columns
`date | PR | WO id(s) | surface touched | what changed | post-campaign observable`. Row
number N is the row's 1-based position in the table, cited by the PR's eval-impact line.
The ledger is the machine-checkable answer to "what is the first post-campaign eval run
actually validating?" — it is walked row by row before the new baseline is blessed.

### Sunset clause

Invariant 8 is restored **the moment the eval restarts** — step 0 of the post-campaign
restart protocol, human-executed (agent autonomy ends at "last fix PR merged"). No
CLAUDE.md edit is needed, because the invariant's text never left; and since `evals.yml`
was never a required check, no repo-setting change accompanies the sunset either. Before
the accepted run is blessed as the new baseline (`make baseline`, a deliberate act), the
eval-debt ledger is walked row by row and every post-campaign observable is checked. The
superseded baseline is retained, not deleted (invariant 9). This ADR then stands,
unedited, as the historical record of the window.

The only escape during the freeze is an **owner-authorized, recorded one-off sanctioned
run**. Agents cannot self-authorize one; the authorization and the run's artifacts are
recorded, not ad hoc.

### Why the alternatives lose

**Option 1 — keep the gate in force.** Satisfying it means executing the runner, which the
maintainer shut down. A mid-campaign run would measure a half-fixed agent with a
mid-repair instrument against a stale pin: evidence-shaped, but not evidence. Every
behavior-surface work order would stall behind a measurement that cannot mean anything
until the re-pin.

**Option 2 — canned green as satisfaction.** Vacuous by construction: the canned job
exercises the harness plumbing on fixtures, it does not measure behavior. Presenting its
green as invariant-8 satisfaction is the exact pathology the audit flagged (32/37
scenarios silently degraded under a green banner). This option launders the gate rather
than suspending it.

**Option 3 — delete the text for the window.** A quiet drop is indistinguishable from an
oversight in six months, and deleting the line destroys the anchor the sunset restores.
The campaign's own rule is that overwritten written rules are recorded as dated decisions
with expiry — which is a suspension under an ADR, not an edit.

### Consequences

Positive:

* The eval-source half of the campaign merges freely: editing the harness never collides
  with a gate that would need to execute it.
* The weakening is a dated, expiring, written record. A future reader finds one ADR, not a
  mysteriously missing invariant.
* Evidence integrity through the window is checkable per PR: zero new files under the
  append-only dirs, `baseline.json` byte-identical.
* The restart run has a defined meaning before it happens: every un-gated behavior change
  carries a named, falsifiable observable in the ledger.

Negative:

* Behavior regressions can merge undetected until the post-campaign run. Mitigation: the
  compensating controls above per PR, and every such change has a ledger row to bisect
  against if the batch acceptance goes red.
* The ledger is convention, not machine-enforced. Mitigation: the mandatory eval-impact
  line makes omission visible in review; a surface-touching diff without a ledger row is a
  review-blocking defect.
* The canned job's green could be mistaken for eval evidence. Mitigation: the naming ban —
  it is never called "passed the regression eval suite" — and the mandated verbatim
  eval-impact wording.

Revisit trigger: an owner-authorized, recorded one-off sanctioned run is the sole
mid-window exception, per the sunset clause above. If the campaign is abandoned or
restructured such that the post-campaign run will not happen, this ADR must be superseded
by a new one rather than left standing.

## More information

* [ADR 0004](0004-eval-first-development-and-regression-gating.md) is the founding record
  of the gate this ADR suspends. It stays accepted and is not superseded: the mechanism is
  untouched and resumes at the sunset.
* Invariant 9 (eval artifacts are append-only) is not weakened by this ADR; the freeze
  upholds it trivially.
* CLAUDE.md lines cited (all left unedited): 24 (invariant 8), 292 (Definition of Done
  item 3), 294 (Definition of Done item 5), 304 (`make eval-reg` working rule).
* [`docs/eval-debt.md`](../eval-debt.md) — the ledger, created with this ADR.
* Numbering: this ADR takes 0011 because it lands first, in campaign Phase 0. Three
  campaign work orders (WO-C1-01, WO-C3-02, WO-C4-02) had each earmarked "ADR 0011" for
  their own decisions; they renumber to next-free-at-landing.
* Decision source: the maintainer's fix-campaign briefing of 2026-08-08, section 0a ("The
  eval is frozen"), which this ADR enacts in-repo.
