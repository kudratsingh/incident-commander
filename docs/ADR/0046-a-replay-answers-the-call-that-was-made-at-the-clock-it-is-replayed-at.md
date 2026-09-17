# ADR 0046: A replay answers the call that was made, at the clock it is replayed at

* Status: accepted
* Date: 2026-09-17
* Decider: WO-R3-197 (plan v2.1 Phase 3, WP-3.2)

## Context and problem statement

[ADR 0043](0043-a-recording-is-keyed-by-what-the-agent-sends.md) settled what a recorded world *is*: the platform's own answers, keyed by the wired arguments the agent sends, with the evaluator's answer key in a sibling file. It left three questions to whoever built the client that replays one, and all three decide whether a recorded run is a measurement or a well-formatted guess.

**What does a replay do when the recording cannot answer?** A recording is finite — the recorder takes the dossier's expected reads, the scenario's own precondition calls, and every read tool crossed with the values the scenario names. An agent is not finite: a strategy under test may probe something none of those cover. There has to be an answer, and the wrong one is the cheap one. This project already paid for that mistake once in the other direction: an empty database and a seeded one were indistinguishable through `tools/list` for the life of the project, because "no rows" is what both look like.

**What does a replay do with time?** A recording is taken at one moment and replayed at another — that is the whole point, and the gap is days or weeks. Every DLQ row carries `created_at` and `dead_lettered_at`, the lag reading carries `measured_at` and `age_seconds` (v0.6.7, plat #204), a paused DAG carries `paused_expires_in_seconds`, a cache key carries `ttl_seconds`. An agent reasons about all of them: "this row died four minutes ago" is evidence, and "this row died eleven days ago" is different evidence about a different incident. A replay that hands over the recorded strings unchanged is handing the agent a world whose every age is wrong by the age of the recording.

**What does a replay do when the agent calls a Tier-1 tool?** A recording holds read results only — `evals/dossier.py::_read_tool` makes that true by construction — so there is nothing to answer with. But "nothing to answer with" does not settle what should happen, and the two obvious options land in very different places.

## Decision

**1. A miss is a counted, structured error. It is never an absence, and it is never smoothed over.**

`RecordedMCPClient.call_tool` returns a `ToolResult` with `is_error=True` and a payload whose `error` is `not_recorded`, appends a `ReplayMiss` carrying the tool, the `call_key` that was looked up and why, and the run reports the count. A run with one or more misses is `degraded` in [ADR 0013](0013-run-provenance-is-part-of-the-eval-result.md)'s sense: the agent's next step was taken after a tool error the world it is graded against never produced, so the trajectory is not a trajectory through that world.

Two shapes were rejected. An empty-but-healthy result (`ToolResult()`) makes "the recording does not hold this" identical to "the platform has no rows", which is the `tools/list` failure in a new place. Raising makes a miss end the run as a crash, which loses the measurement of *how wide the recording needed to be* — the one number that says whether to re-record.

The miss payload names neither the scenario nor the fault. It is the only part of a replay the agent can read, and a scenario name like `remediate_consumer_lag_success` would hand it the answer — the same reasoning as platform ADR 0012's withheld `chaos:invoke` scope and as `recorder.lint_agent_visible_vocabulary`. It does say that a replay produced it, deliberately: a miss dressed as a platform error would make a run that measured nothing indistinguishable from a run that met a broken dependency.

**2. A recording is the world as it was, NOW: absolute clocks are shifted to the replay clock by one rigid offset, and relative durations are held.**

At load — once, not per call — every recorded result is re-based. `SHIFTED_CLOCK_FIELDS` names the absolute timestamps per tool and adds `replay_clock - provenance.recorded_at` (rounded to whole seconds) to each. `HELD_DURATION_FIELDS` names the durations that are already expressed relative to the moment of the reading — `age_seconds`, `ttl_seconds`, `paused_expires_in_seconds` — and leaves them exactly as recorded. The two halves are one rule, not a rule and an exception: the replay clock *is* the moment of the reading, so a reading that was twelve seconds old when it was taken is twelve seconds old now, and `now - measured_at == age_seconds` holds at replay exactly as it held at record time.

Both lists are **written down per tool**, with the reason for each entry, rather than pattern-matched on field names. The judgement an entry encodes is not "is this a datetime" but "is this a clock of the world the agent is looking at" — a returned maintenance window would be a datetime that must *not* move when the reading does. `tests/unit/test_recorded_client.py::TestTheClockIsRebased` derives the expected coverage from the tool registry's output models and asserts the written list covers it, so a time field shipped tomorrow fails a test rather than silently replaying a stale timestamp.

The offset is **rigid and document-wide**, anchored at the recording's own `recorded_at`. Anchoring per call — each reading "as fresh as it was" — was rejected: the same DLQ row read through `list_dlq_messages` and through `get_trace` would then carry two different `created_at` values, and a recorded world that contradicts itself is a fixture defect rather than a benchmark (plan 02 § 187, and the rem-4 run that produced the coherence lints).

Decrementing the held durations by the offset was also rejected. A recording replayed a day later would hold an expired pause and a negative age — a world nobody recorded, and one in which every temporal scenario is trivially over.

The residual is stated rather than hidden: a recording is read over a window (`remediate_consumer_lag_success` took eight precondition attempts), so an age recomputed from a re-based `measured_at` can differ from the held `age_seconds` by up to the recording session's own duration. That residual was already in the recording — the recorder read an interval and a replay flattens it to an instant — it is bounded, and it is smaller than the incoherence the alternatives buy.

A result with no declared clock, and any result replayed at its own recording moment, is byte-identical to what the platform sent. The fewer bytes a replay changes, the less there is to argue about later.

**3. A Tier-1 call is refused by `tier_of`, and the refusal raises.**

Refusal is classified by `policies.tier_of` over the whole registry, never by a name list — `policies.py:403-414` records what happened the last time tier classification was inferred rather than declared, and a name list here would go stale the first time the platform ships a tool. A tool that is not in the registry at all is refused too: a call whose tier cannot be established is not a call a recording may answer.

It raises `ReplayRefused`, which is deliberately **not** an `MCPError`. Transitions catch `MCPError` and escalate with a reason, so an `is_error` or an `MCPError` here would end the run as an ordinary, plausible-looking escalation — and a run that *could not have remediated anything* would read as a run that *chose* to escalate. That is exactly the run mistakable for a more-real run than it is, which ADR 0013 exists to forbid. Safety is graded from the platform audit log as ground truth (invariant 6), and a recording has none, so there is no honest answer to give.

This is defence in depth, not the guard. `investigation._execute_probe` re-checks `tier_of` before every probe and escalates on a non-read tool, and WP-3.3 stops a scenario with `expected_action_tools` at the `PLANNING` handoff, so in a correct recorded run this refusal never fires. It exists for the run where something upstream is wrong.

**4. The replay client never falls back to a real client, and cannot.**

`evals/recorded_client.py` imports no `Settings`, no URL, no token and no transport — the one name it takes from `mcp_client` is `ToolResult`, asserted on the module's own syntax tree. It refuses to construct without a recording and refuses an empty one, rather than answering `not_recorded` to everything and producing a run-shaped object with nothing in it. `make_client`'s refusal to resolve an empty token upward (S-04) is the precedent; ADR 0013 is the reason.

## What was rejected

**Wiring at lookup time instead of at record time** — already rejected by ADR 0043, and the same answer applies on this side: the client wires the incoming arguments through the same `wire_arguments` and computes the key with the recorder's own `call_key`, so there is one definition of "the same call" in the repo. `replay_key` is a lookup helper, not a second normalisation; the test asserts the module defines no key function of its own.

**Putting the replay client in `evals/fakes.py`.** A fake is a per-tool queue of hand-written answers. A recording is the platform's own answers looked up by the exact call. Keeping them in one file is the first step to conflating the two claims a run makes about itself.

**Rebasing per call, lazily.** Answers are built once at construction, so answering is a dict lookup, nothing is recomputed while a run is in flight, and two runs of one world in parallel cannot interfere — which is what WP-3.3's parallel-safety requirement rests on.

## Consequences

* A recorded run reports three numbers beside its grades: answered, missed, refused. The first two are the width of the recording, and a re-record is the fix for the second.
* A recording whose stored `key` no longer matches its own arguments fails to load into a client. The key is stored rather than derived precisely so that can be checked; a replay that trusted the stored key would answer one call with another call's answer.
* The re-basing tables are a maintenance surface with a guard: a platform release that adds a time field to a read tool will fail `TestTheClockIsRebased` until someone decides whether it is a clock of the world or a duration of the reading. That is the intended cost — the alternative is deciding it by accident.
* Nothing here spends money. A recorded run spends model tokens on the agent's own planner calls, which is WP-3.3's business; this module has no model client and no network.
