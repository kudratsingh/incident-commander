# ADR 0051: A scenario family shares one alert that names the stuck pipeline, and its noise is a real thing the world holds

* Status: accepted
* Date: 2026-09-17
* Decider: WO-R3-202 (plan v2.1 Phase 4, WP-4.3)

## Context and problem statement

The `jobs_not_progressing` family is the first real scenario family in this
corpus: four worlds that share one observable symptom — jobs accepted, nothing
executing — with three different answers. Plan 01 § 10 states the bar a family
has to clear: *agent-visible evidence infers the truth, no single field leaks the
label, and the discriminating evidence requires investigation rather than the
alert text.* Plan 03 § 13 makes the first half a review failure: "alert text
gives the answer, or only one tool is plausible from the wording".

Nothing in the corpus said how to satisfy that, and the corpus's own history
says the two obvious answers are both wrong.

**Give each world its own alert** and the family stops being a family. Every
scenario in this repo before now carried an alert written for its own fault —
`consumer_stalled`, `cache_miss_spike`, `post_deploy_error_spike` — and each of
those names the answer closely enough that the investigation is a formality. Four
such alerts over four worlds would measure whether the agent can read.

**Give them a subjectless alert** and the investigation becomes a guess. An alert
that names no resource leaves `investigation.alert_subject` inert by design, and
the corpus has paid twice for what happens then: the agent chased a visible
billing alert instead of the group it was paged about (2026-08-30/31), and later
scoped a mixed queue by inference and escalated work it could have done
(remediation 7 run B, archive `06e14be3e7b1`, ≈$0.10) — "an alert that names its
subject only inside a fingerprint string gets scoped by inference, and inference
varies".

There is a second question underneath, and it is what actually forced a decision.
WP-4.3 asks the family for "a noise variant with an unrelated deploy marker".
The platform ships a chaos hook for precisely that, `bad_deploy`, and it cannot
be used: its alert carries `source="chaos:bad_deploy"`,
`title="Simulated bad deploy"` and an `extra_data` block naming its own label and
TTL, and `list_active_alerts` returns all three verbatim
(`backend/app/mcp/tools/chaos/bad_deploy.py:98-101`; plan
`DIVERGENCES-2026-09-15.md` G5). Putting it in front of the agent hands it the
word `chaos` and fails the ADR 0012 substring sweep. G5 also records why renaming
the source is not a small change: `scripts/reset_eval_state.py` resolves those
alerts on a `source LIKE 'chaos:%'` predicate, so a rename without moving the
predicate leaves every `bad_deploy` alert surviving every reset as a permanent
distractor — WO-R2-131's closed failure, reintroduced.

So the family needed a rule for its alert and a distractor that is not a lab
hook.

## Decision

**1. Every scenario in a family carries the SAME alert, and the alert names the
pipeline that is stuck rather than the component at fault.**

The `jobs_not_progressing` alert is one payload, shared field for field by all
four worlds:

```yaml
source: platform.jobs
severity: critical
fingerprint: jobs_accepted_not_executing
consumer_group: worker-dispatcher
summary: jobs are being accepted and are not executing; the worker-dispatcher pipeline is not making progress
```

`consumer_group` is a key in `investigation.ALERT_SUBJECT_PROBES`, so the alert
has a structural subject: the agent is required to read
`get_consumer_lag(worker-dispatcher)` before it can hand off (cmd #177), and
`evals/dossier.py::derive_probes` derives that probe mechanically instead of
reporting "no subject probe to derive".

The distinction that makes this work is between *the pipeline that is stuck* and
*the component at fault*. Naming the pipeline is true in all four worlds: in the
dispatcher world the named consumer IS the fault; in both outbox worlds it is
measurably healthy and jobs bound for it still are not executing; in the control
nothing is wrong. Naming the component at fault would be the label, leaked.

The consequence is the property the family exists for: **the mandatory first
probe is a discriminating read rather than a dead end.** A flat, known zero under
an accepted-but-not-executing alert is not an absence of information — it is the
first half of the diagnosis, because it moves the question upstream of the
broker.

**2. Two worlds with different answers may not differ in their agent-visible
alert. A world may add a field the alert did not carry only if that field names
something real that does not imply its own world's answer.**

The noise variant adds `deploy_version: v0.4.3` and nothing else.
`tests/unit/test_policies.py::TestJobsNotProgressingFamily` enforces both halves:
the alerts are equal as dictionaries once declared non-discriminating fields are
removed, one alert covers three different ground truths, and any field present on
some worlds and not others must appear on a world whose answer another world
without it also has — otherwise the field IS the discriminator, dressed as noise.

**3. A family's distractor is a real thing the world holds, never a simulated
one, and it is never graded.**

`v0.4.3` is the newest prod release in the platform's own seeded
`deploy_markers` table. An agent that goes and looks finds a release that
exists, that shipped roughly two hours before the first undelivered event, with
healthy delivery in between — and a sibling row annotated "correlated with
billing failures" that `deploy_correlation`'s whole scenario turns on. Every word
of it is true and none of it caused the incident. Enriching an alert with the
running release is also how production alerts really arrive, so the noise is
realistic rather than contrived.

Nothing about `get_deploy_history` is asserted in the noise variant's
expectation. Ruling the release out is diligence, not correctness: an agent that
ignored the noise and read only the two signals that matter took the best
trajectory available, and a claim on the deploy read would grade it red.
PROTOCOL step 4 cuts both ways — the laziest passing trajectory must be the
correct behaviour, and the claim set must not demand more than correctness does.

`bad_deploy` is **excluded from agent-visible scenario families** until its
source rename and `reset_eval_state.py`'s `source LIKE 'chaos:%'` predicate move
together (G5). This is a decision about every future family, not only this one.

**4. A noise variant is its own template, not another seed of the template it
varies.**

Its world and its ground truth are identical to the quiet variant's, and both end
in the same terminal state, so OUTCOME, ACTION and SAFETY cannot separate them —
only ROOT_CAUSE can, because `deploy_regression` is also outside `FIX_MAP` and an
agent that blames the release escalates with the right terminal state and the
wrong answer. That is the measurement. Reports group by template (ADR 0039), so
making the variant a second seed of the same template would average the noise
effect into the number it is supposed to isolate.

## Consequences

**What this buys.** A family where the alert is provably not the answer, checked
by a test rather than asserted in a PR body; a mandatory first probe that is
useful in every world; and a noise measurement that isolates one variable —
`deploy_version` present or absent — with everything else held equal, which is
the shape plan 03 § 12's paired comparisons need.

**What it costs.** The alert's `deploy_version` field is a top-level scenario
convention the platform's webhook does not send, so it joins the twelve already
recorded in
`tests/unit/test_scenario_alert_premise.py::_NON_WEBHOOK_ALERT_FIELDS`. A real
alert would carry it inside `extra_data`, which `alert_subject` already reads one
level into, so nothing changes when the platform starts sending it.

**What it does not decide.** Whether `bad_deploy` should be repaired. This record
excludes it from agent-visible families and says what would have to move for that
to change; doing it is platform work with a coupling the plan already documents.

**The rule generalises, and the next two families are where it will be tested.**
Family C (`workflow_stuck`, WP-7.2) has the same shape — a dead-lettered root, a
paused DAG and a stalled resolver all present as "the child never ran" — and its
alert will have to name the chain rather than the mechanism. `api_latency` the
same.

## Alternatives considered

**One alert per world, structurally similar but not identical.** Rejected: "similar"
is not checkable, and the 2026-09-07 lesson is that anything the alert implies
gets used. Equality is the only version of this a test can hold.

**A subjectless family alert.** Rejected on two live findings (see above). It would
also leave `derive_probes` reporting "no subject probe to derive" for every member,
which is the dossier telling you the family cannot be reviewed.

**`bad_deploy` with the leaking fields stripped at the scenario boundary.** There is
no such boundary: `list_active_alerts` is a platform tool and the scenario cannot
filter it. Stripping would mean a platform change, which is G5's own conclusion.

**The noise variant as seed 1 of the outbox template.** Rejected under point 4:
the grouping would hide the effect being measured.
