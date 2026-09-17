# ADR 0048: Constrain the selector with its schema, not with a temperature

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh

## Context and problem statement

WP-6.1 adds the `candidate_selector` — the first genuinely new LLM role in the system, and the
one the buildout's headline question turns on. Plan 04:161 specifies it as "`SelectionResult` per
02 § 12; temperature 0; validators", and plan 02 § 12 spells the schema out.

Two things in that specification cannot be implemented as written.

**Temperature.** Anthropic removed the sampling parameters on its newest model families, and
`llm/client.SAMPLING_REJECTED_MODELS` lists the ids that reject `temperature` with a 400. A
selector that *required* `temperature=0` would make the headline experiment un-runnable the day
the repo is pinned to one of them, and the failure would be a 400 on the first selector call —
nothing to do with selection. Owner decision O-24 (2026-09-17) says as much: nothing built now
may require a sampling parameter.

**What `probe_more` points at.** Plan 02 § 12's schema, and WO-R3-208's own acceptance, say
`selected_candidate_id` is `None` exactly when the decision is not `select`. Plan 02:239 says
"if decision == `probe_more`, the selected candidate's `next_probe` is emitted". If `probe_more`
carries no selected id there is no selected candidate to take a probe from, so one of the two
sentences has to give.

## Decision drivers

* O-24: nothing may require `temperature` on a model call made from here on.
* Plan 02 § 12's stated purpose for temperature 0 is a selector that does not wander — a
  reproducible decision, not a particular API field.
* WP-6.3 calibrates this role's `uncertainty` against whether it was right, so every number the
  selector reports needs a scale a second run can be compared on.
* Architecture principle: lead with the structural fix. A property enforced by a validator is a
  property that cannot be half-followed; a property asked for in prose is a preference.
* A strategy must be able to read "the candidate this decision points at" in one place. A rule
  re-derived at each call site is a rule that will be derived two ways.

## Considered options

1. Send `temperature=0` as the plan says, and accept that the arm breaks under a newer pin.
2. Make the temperature configurable, defaulting to none.
3. Send no temperature, and buy reproducibility from the schema (chosen).

For `probe_more`, separately:

1. Require `selected_candidate_id` on `select` **and** `probe_more`, `None` only on `escalate`.
2. Keep the `None`-unless-`select` rule and derive the probe from the ranking (chosen).

## Decision outcome

**The selector's call sends no temperature, and there is no parameter to send one with.**
`select_candidate` takes no `temperature` argument, so a later caller cannot reintroduce one
without editing this decision. What the plan wanted from temperature 0 is bought structurally
instead, by four properties of `SelectionResult`:

* a closed decision set — `select`, `probe_more`, `escalate`, and nothing else parses;
* an id space bounded by the candidate set — every key of `scores` resolves to a candidate the
  selector was shown, and every candidate it was shown is scored, both failures naming the id;
* a declared scale — `scores` and `uncertainty` are in `[0, 1]`, so 0.8 means one thing;
* forced tool use against a fixed schema, as every structured call in this repo already makes.

**A selection is stated exactly when there is one.** `selected_candidate_id` is `None` if and
only if the decision is not `select`, enforced in both directions.

**On `probe_more`, the probe comes from the highest-scored candidate.** `SelectionResult`
carries a `chosen_candidate_id` property that is the single spelling of "the candidate this
decision points at": the named candidate on `select`, the top-scored one on `probe_more`, and
nothing on `escalate`. Ties go to the first such id in `scores`, which is the order the model
wrote them in. WP-6.2 reads that property; it does not re-derive the rule.

### Why the alternatives lose

**Sending `temperature=0` anyway.** It is correct today — the repo is pinned to
`claude-sonnet-4-6`, which accepts it — and wrong on the first re-pin. The whole point of the
selector is a comparison that survives a model change, and a parameter that 400s on the newer
family makes the arm the first thing to break. `SAMPLING_REJECTED_MODELS` exists because
WP-5.3 hit this; repeating it in the role the headline number depends on is worse.

**A configurable temperature defaulting to none.** Tempting, and it is what `best_of_n_sampled`
does — but that arm's whole subject *is* sampling, so the knob is the experiment. Here the knob
would be a way to reintroduce the failure by configuration, on a role whose determinism is
supposed to be a property rather than a setting. If a future experiment wants to sample the
selector, it is a new arm with its own ADR, not a field on this one.

**Requiring `selected_candidate_id` on `probe_more`.** This is the reading that keeps 02:239
literal, and it is a real option — the fallback if the derivation turns out to mislead. It loses
because "selected" then means two different things in one field: on `select` it is a commitment
the run acts on, and on `probe_more` it is a preference the run does not. A reader of a
trajectory, and every report that groups on the field, would have to know the decision to know
which. The rejected shape is also the one that reads as a bug in a report: a `probe_more` row
carrying a selected candidate looks exactly like a run that committed and then failed to act.

### Consequences

Positive:

* The selector runs unchanged under every model pin, including the families that reject sampling
  parameters. Nothing about the headline experiment depends on an API field.
* `scores` is a total ranking of the set, so `selected@k` and the oracle gap (WP-6.2) are
  computed from a complete mapping rather than from a partial one with holes.
* Reproducibility is asserted by tests that stay true: "no temperature is sent" and "no
  parameter exists to send one" are checkable under any pin, where "temperature is 0" would have
  to be deleted at the next re-pin.

Negative:

* The selector is not literally temperature-0, so two selector calls on the same context may
  differ. Mitigation: the schema bounds *what* can differ — the decision set, the id space and
  the scale are closed — and WP-6.3's calibration report measures the variation rather than
  assuming it away. No selector number is reported before that report exists (plan 02:243).
* Requiring every candidate to be scored costs a repair when the model omits one. Mitigation:
  the prompt says it in as many words, the failure names the missing id, and ADR 0035's one
  re-ask carries that message; the alternative was a ranking with silent holes.
* `chosen_candidate_id` is a derivation, so a `probe_more` whose top score is a near-tie emits
  the probe of a candidate the selector barely preferred. Mitigation: `uncertainty` is on the
  record beside it and is what the calibration report reads; the tie-break is the model's own
  stated order, and it is deterministic.

Revisit trigger: a model family that makes structured output non-reproducible enough that two
selector calls on one context disagree about the *decision* (not the scores) — measured by
WP-6.3's calibration report. That is the condition that makes a sampling parameter worth having
again, and it is also the condition under which option 1 above becomes the right fallback.

## More information

* Plan `docs/plans/research-buildout-v2.1/02_INCIDENT_COMMANDER_PLAN.md` § 12, `04` WP-6.1.
* Owner decision O-24 (sampling parameters and model choice), 2026-09-17.
* [ADR 0035](0035-a-parse-failure-of-our-own-output-is-a-harness-event.md) — the one bounded
  re-ask that makes a refused selection repairable rather than fatal.
* [ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md) — why no ground
  truth can reach this role's context.
* [ADR 0042](0042-a-candidates-evidence-reference-is-resolved-by-a-validator.md) — the same
  bound-context mechanism, one layer down, and the argument for it at length.
* [ADR 0045](0045-a-sampled-step-is-one-samples-and-every-draw-is-charged.md) — where the
  sampling-parameter problem was first hit.
* INC-002 (`audit-ws/context/INCIDENTS.md`) — why this role reads its evidence through the same
  renderer as the briefing writer and the briefing judge.
* Implemented by WO-R3-208.
