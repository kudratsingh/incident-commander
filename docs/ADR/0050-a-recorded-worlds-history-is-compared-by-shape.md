# ADR 0050: A recorded world's history is compared by shape, and the scenario's own claims are what hold it

* Status: accepted
* Date: 2026-09-17
* Decider: WO-R3-271 (follow-up to plan v2.1 Phase 3, WP-3.3)
* Amends: [ADR 0047](0047-a-recorded-run-grades-diagnosis-and-the-plan-and-nothing-else.md) § 5

## Context and problem statement

[ADR 0047](0047-a-recorded-run-grades-diagnosis-and-the-plan-and-nothing-else.md) § 5 made `make world-drift` a required step: "a recorded result is not evidence until the drift check passes". That is the right rule and it made recorded mode unusable within hours of shipping.

The v0.6.9 re-pin (cmd #281, 2026-09-17) ran the check on all four committed recordings, on a freshly reset world, about two hours after they were taken. Every one exited 1, with 217 to 262 disagreements, and **every single disagreement was inside one of two tools**:

* `list_audit_events` — `total` had grown 3,770 → 3,946, so the newest-50 page returns different rows. Reads *are* audit events: the harness's own probes move this number, and the drift check's own re-read moves it again while it looks.
* `search_traces` — job and trace ids, re-minted by the resets in between.

Not one disagreement touched a field v0.6.9 changed, which is what rules the platform release out as the cause: v0.6.9 added two tools and moved no existing schema, and no recording holds a call to either.

The three readings ADR 0047 § 5 offers — a platform release, a fixture-pack change, a dirty world — do not contain this one, and none of their remedies reaches it. **No reset undoes history.** Re-recording produces a recording that is stale in exactly the same way an hour later. So under the rule as written, no recording of this platform can ever pass its own drift check, and "a recorded result is not evidence until the drift check passes" quietly becomes "a recorded result is never evidence".

A check that can only ever fail is worse than no check. Nobody reads its output twice, and the one time it has something real to say — a platform release that moved a field the scenario grades on — it says it in the 240th line of noise.

There is a second problem underneath, and it is the one that decides the shape of the fix. `fixture_drift._VOLATILE` already asks a question of every field: *does the fixture pack FIX this value?* A seeded `lag: 29` is fixed, so a recording must match it. A `ttl_seconds` countdown is not, so only its type is checked. That question is necessary for a recorded world and it is not sufficient, because a recording is replayed against a world that has been **reset** since. The audit log survives the reset; the row ids do not.

## Decision

**1. A per-tool, per-path table of HISTORY paths, compared by shape and never by value.**

`evals/world_drift.py::_HISTORY` names them, each with a written reason: `list_audit_events`' `total` and its event rows' server-minted columns, the same audit rows read through `get_trace`, and `search_traces`' `trace_id` and `job_id`. Shape means presence, JSON type, and the rows a scenario's own claims read — nothing else.

The membership test is stated as a test rather than a family resemblance, and it is deliberately **not** `_VOLATILE`'s: *can anything in the lab put this value BACK?* A path that only fails "does the seeder fix it" belongs in `_VOLATILE`. A path that fails this one belongs here. Two facts about this platform put the three families above on the far side of it:

* the audit log is immutable (CLAUDE.md invariant 6 — safety is graded from it, so it had better be), and every read the harness makes is an entry in it, so it grows on its own while nothing else happens;
* `make eval-reset PURGE_IDEMPOTENCY=1` deletes the fixture rows and seeds new ones, so a server-minted row id is a different id afterwards even when the row means the same thing.

**2. The table lives in `world_drift`, not in `fixture_drift._VOLATILE`.**

`fixture_drift.compare` grew one keyword-only argument, `shape_only`, which is **empty by default**. `make test-drift` and `tests/integration/test_canned_fixtures_match_live.py` pass nothing, so the walk over the 41 committed canned fixtures is the walk it was, byte for byte, and widening `_VOLATILE` is still the only way to forgive a canned fixture.

That separation is the point, not tidiness. `_VOLATILE` answers a question about a hand-written fixture that a person will re-record; `_HISTORY` answers a question about a live reading that no person can reproduce. One table serving both would mean every future entry weakened two checks, and the one that forgave more would win by accident — the exact argument `world_drift`'s own docstring makes for reusing the walk instead of writing a second one.

**3. A path a scenario's own claims read is not forgiven — it is RE-CHECKED against the live reading.**

This is the half that makes the forgiveness safe rather than merely narrow. `rechecked_claims(scenario)` derives, from the scenario itself, every `expected_evidence_fields` claim that reads a declared history path, and `drift_between` asks the scenario's own question of the live answer: not "is this the same id?" but "does the live world still satisfy what this scenario grades on?". A broken claim is a drift of its own kind, `history_claim_broken`.

`failed_traces_scan` is the live example: it grades `search_traces.matches[].trace_id is_null: false`. A re-minted id satisfies it, so that is not drift — but an empty `matches` list does not, and that is drift the 47 id disagreements would have buried.

The comparator is the grader's own `FieldComparator.satisfied_by` over the grader's own `selected_values`, so "the claim holds" means here what it means at grade time. Derived rather than listed, for `dossier.derive_probes`' reason: a scenario that grows a claim on a history path is covered the moment it lands.

`which: sum` claims are the one shape not re-checked, and they do **not** lose their guard: `sum` reduces every observation across a whole RUN to one total, and a single live reading is not a run, so re-checking it here would be a second and weaker definition of a claim the grader owns. `shape_only_paths` removes their paths from the forgiven set instead — a value that cannot be re-checked goes back to being compared by value, which is the fail-safe direction.

Preconditions need nothing: `main` already establishes every one of them live, and exits 7 when one is unmet. A precondition on a history tool is checked harder than a claim, not softer.

**4. The report says what it did not compare, clean or not.**

`DriftReport` carries `shape_only` and `rechecked`, and `render` prints both on every run. "No drift" must never be readable as "everything was compared" — the same reason ADR 0047 § 2 marks a not-applicable dimension twice, applied to a check rather than to a grade. A reader of a clean drift report can see the path list and argue with it.

**5. Three mechanisms keep this from growing into a blanket exemption**, and none of them is "somebody remembers" (`tests/unit/test_recorded_mode.py::TestHistoryIsNotState`):

* every declared path is checked against that tool's own **output model**, so a typo, or a path the platform dropped, is a failing test rather than a wider hole — ADR 0046's `TestTheClockIsRebased` pattern, for the same reason;
* the declared set must be a **strict subset** of the model's paths, so no tool can be forgiven whole. `search_traces` keeps `job_type` and `status`, which are the fault's signature; `list_audit_events` keeps `events` itself, so a listing that lost all its rows still drifts as `no_live_rows`;
* the set is **pinned by exact equality**, so widening it is an edit somebody reviews rather than a side effect of touching the file.

And the safety net is asserted directly, because the whole risk of a rule like this is that it works: a changed dead-letter row, a changed `lag` reading, a changed trace `status`, a dropped audit field and a retyped audit id are each proved to still fail. Those five tests pass before this change and after it, which is what makes them a net rather than decoration.

## What was rejected

**Not recording the history tools at all, and answering them `not_recorded` (the order's option b).** It trades a check that always fails for a measurement that always degrades. ADR 0047 § 4 sets `degraded` on any run with a miss, precisely because "the agent took its next step after a tool error the world never produced" — so a replayed agent that reads the audit log or searches traces, which is ordinary investigative behaviour, would produce a non-comparable row. Recorded mode exists for paired comparisons in phases 5, 6, 9, 12 and 13; a mode whose runs are non-comparable whenever the agent reads widely is not that mode. It also reverses ADR 0043's sweep, whose entire stated purpose is that "a replayed agent that reads more widely than the expected set gets an answer instead of a `not_recorded` miss", and it would not even solve the problem for the scenarios whose claims *do* need those tools — `failed_traces_scan` and `trace_investigation` would be back where they started.

**Declaring the two tools volatile in `_VOLATILE`.** One line, and it weakens `make test-drift` over 41 committed fixtures to fix a recorded-world problem. Different question, different table.

**Forgiving `search_traces` wholesale.** `job_type` and `status` are the fault's signature — `dead_letter` where the seeder wrote `dead_letter` — and `remediate_dlq_backlog_success` rests on them. Only the two id columns are history. The measured proof is in the test: against a second honest reading of the same stack, the forgiveness removes 94 id disagreements and keeps all 9 `job_type`/`status` ones.

**Dropping `total` from the audit listing's output model, or filtering the harness's own reads out of it.** Both are platform changes to make a commander-side check convenient, and the second is worse than inconvenient: a read that does not appear in the audit log is a hole in the log that invariant 6 grades safety from.

**Making the drift check's verdict advisory — exit 0 with a warning.** The ADR 0047 § 5 rule is load-bearing and ADR 0047 was right to make exit 1 mean "the world moved". The problem was never the verdict; it was that the walk was answering a question about history as though it were a question about state.

**Comparing history paths by row COUNT instead of by value.** Tempting for `events`, and wrong: the page size is 50 whatever happened, so the count is constant and the check would be vacuous — a green that says nothing, which is the shape ADR 0047 § 2 spends a page refusing.

## Consequences

* ADR 0047 § 5 stands with a fourth reading added to its three, and `docs/runbook.md` names it: *the world's own history moved, and no reset undoes that*. Tell it apart by asking which tools the disagreements are in — a release moves the tool whose schema moved; history moves the audit listing and the trace ids and nothing else. That reading is now the check's own default output rather than something a reader has to reconstruct.
* The four committed recordings become reportable again once a drift run confirms it. **That run is free and has not happened** — it needs the stack, so it is a deferred item, exactly as ADR 0047 § "Consequences" already says of the first drift run.
* `_HISTORY` is a maintenance surface with the same guard as ADR 0046's re-basing tables: a platform release that adds a column to an audit row fails the coverage test until someone decides whether it is history or state. That is the intended cost — the alternative is deciding it by accident.
* A recorded world's audit log is now, in effect, not compared at all beyond its shape. That is the honest position: it is a log of what happened to the world before the reading, the reading itself changes it, and there is nothing a recording could claim about it that would still be true. What a recording still claims about it — that the tool answers, with rows, of the declared shape — is what a replayed agent actually consumes.
* Nothing here spends money. The walk is pure, the tests are hermetic against the committed recordings, and the two-observation proof reuses two recordings this repo already carries.
