# ADR 0043: A recording is keyed by what the agent sends, and its answer key is a sibling file

* Status: accepted
* Date: 2026-09-17
* Decider: WO-R3-196 (plan v2.1 Phase 3, WP-3.1)

## Context and problem statement

Phases 5, 6, 9, 12 and 13 of the research buildout are all **paired comparisons**: two strategies, or two prompts, or a judge against itself, measured on the same incident. The repo has two ways to give the agent a world and neither can carry that measurement.

`canned_tool_responses` is a per-**tool** sequence (`evals/scenarios/schema.py`): the first `list_dlq_messages` gets the first entry whatever arguments were sent. So a strategy that reads a tool three times and one that reads it once see different worlds, and a strategy that reads one tool with two different arguments gets the same answer twice. Best-of-N, search over candidates and judge calibration all need argument-awareness, and a sequence cannot have it.

A live run has the world but not the repeatability. [ADR 0020](0020-one-mutating-scenario-per-live-invocation.md) serializes mutating scenarios one per invocation, and no two live runs see the same world anyway — the dead-letter clocks move, lag is a cached gauge with a 60-second cadence, the traffic generator writes rows. "Strategy A beat strategy B" measured across two live runs is measured across two worlds, and the difference between them is not the strategy.

So: read the world once, keep the answers, replay them. The question this record answers is what "the same call" means, and where the answer key goes.

## Decision

**1. A recorded call is keyed by the WIRED arguments, and both the recorder and the replay client compute that key with the same function.**

`recorder.call_key(tool, wired_arguments)` is `<tool> <arguments_hash(wired)>`. The arguments must be the output of `tools/wire.py::wire_arguments` — validated against the tool's input model with every optional field default-filled, because that is what the platform's contract says a request is (platform ADR 0010). The recorder wires each probe *before* it calls it, so the request it makes is the request the agent would make and the key is what it sent.

This is the load-bearing half of the record. `evals/dossier.py`'s probes carry the raw sorted argument tuple, and every call the agent makes goes through `wire_arguments`, so a recording keyed on `list_dlq_messages({})` cannot answer the agent's `list_dlq_messages({"job_type": null, "remediation_hint": null, "limit": 50, "offset": 0})`. The failure mode is not an error: it is a `not_recorded` miss on **every** read, i.e. a benchmark that measures nothing while looking like it ran. This is divergence F2 of the plan verification, and `tests/unit/test_recorder.py::TestKeysAreTheWiredArguments` is red against raw arguments.

`arguments_hash` is reused rather than replaced. It is the commander's implementation of the normalization the platform keys its own idempotency store on — sorted keys at every depth, no whitespace, ASCII-escaped, UTF-8 — so "the same call" means here what it already means on the wire. Inventing a second canonical form would give the repo two answers to one question.

**2. A recording stores the whole `ToolResult`, because that is what a replay client must answer with.**

`world_audit.read` returns a `Reading` — the first JSON object plus the joined raw text — and drops the content blocks and `is_error`. `read_result` is the same read loop with the result still attached, and `read` is now that function with the second half dropped. One loop, two callers: an audit and a recording can never disagree about what a probe returned. This is divergence F3, and the alternative — a second `call_tool` call site inside the recorder — was rejected for exactly that reason.

**3. The evaluator's ground truth lives in a sibling file, and the wall is structural.**

`<scenario>.<stamp>.<id>.json` is the recording; `<scenario>.<stamp>.<id>.truth.json` beside it is the answer key. Three mechanisms, none of which is a rule somebody has to follow:

* `recorder.load_recording` takes one path and has no parameter that could reach the sibling — no `truth=` flag, no directory walk;
* `RecordedWorld` is `extra="forbid"`, so a recording carrying a `ground_truth` key fails to load rather than quietly handing an answer key to a replay client;
* `RecordedWorld.answer` returns a `ToolResult` and nothing else, so the *only* part of a recording a replayed agent can see is a platform response. Everything else in the document — the world label, the findings, the origins — is evaluator-side.

This is [ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md)'s projection applied to a recording, and for the same reason: a wall made of remembering is not a wall.

**4. A recording says which world it is, and its answer key says whether it applies.**

Every recording carries `live_mcp`, `chaos_seeded`, the hooks that built it and the settle wait. The sibling's `applies` flag is computed by `graders/root_cause.py::label_describes_this_world` — the single home of [ADR 0040](0040-a-ground-truth-is-a-statement-about-one-world.md)'s rule — rather than asserted at record time. A recording of an unseeded live world therefore states in its own answer key that the answer key is not about it, which is INC-003 prevented at the moment of recording instead of discovered at grade time on a $2.15 archive.

**5. A recording is evidence.** Registered `recorded_world` and `recorded_world_truth` kinds in `evals/artifacts.py`, written with exclusive-create, never overwritten, resolved through `artifacts.newest` (divergence D2: `KINDS` is a closed registry, so an unregistered family cannot be resolved at all). A second recording of one scenario is a second fact, not a correction of the first — which is what lets `make world-drift` say the world moved.

## What was rejected

**Keying on the raw arguments and wiring at replay time.** It would make the recording's key depend on which caller recorded it, and the replay client would have to reverse the default-fill to look anything up. The wire form is the only form both sides can compute independently.

**Putting recordings under `evals/reports/`.** A recording is an input a later run is *executed against*, not a document somebody reads. `evals/recorded_worlds/` keeps that distinction visible. It is also why the folder is **not gitignored**: WP-3.2's replay client and WP-3.3's `--mode recorded` cannot run in a fresh clone without a world to run against, and a benchmark whose world exists only on the machine that recorded it is not a benchmark anyone can reproduce. Gitignoring them was considered and rejected on that ground — they are not small, so the rule is the same as for `evals/runs/` and the versioned reports: new recordings land untracked and visible, and committing the ones a reported result rests on is a decision a person makes. The hub's `evidence/sync.sh` mirrors the folder either way.

**A single file holding the world and its ground truth, with the loader "just not reading" the truth key.** That is the shape of every leak this project has recorded. The separation is cheap; a projection somebody has to maintain is not.

**Forgiving platform clocks in the fingerprint.** `world_fingerprint` drops only the recorder's own per-call timing fields. A moved `dead_lettered_at` between two recordings is drift — a real difference between two worlds — and which drift is benign is `evals/fixture_drift.py::_VOLATILE`'s question. A fingerprint that forgave them would report two different worlds as one, and the whole point of `make world-drift` is to notice.

## Consequences

* Recorded mode can be built (WP-3.2's `RecordedMCPClient`, WP-3.3's `--mode recorded`) without a second opinion about what a call is: it wraps `RecordedWorld.answer`, turns `None` into a structured `is_error` with reason `not_recorded`, counts the misses and refuses Tier-1.
* A recording is only as wide as the calls it holds. The recorder records the dossier's expected set, the scenario's own precondition calls and every read tool crossed with the values the scenario names — and nothing else, because a probe aimed at an invented resource id reads as evidence and is not. Widening it later is additive; a miss is measured, not silent.
* `evals/dossier.py::_select` grew three vocabulary arguments so `make world-record` refuses with its own words rather than telling an operator to run `make world-dossier`. Defaults keep the dossier's output byte-identical.
* The recorder polls preconditions where the dossier reads them once, and says why in `establish_preconditions`: a dossier is read by a human deciding whether to spend, so a slow seed is information; a recording is replayed, so a slow seed must be waited out. It does not import the runner's copy of that loop, because WP-3.3 makes the runner import this module and that would be a cycle.
* `make world-record` is not free of consequence even though it is free of cost: it seeds the scenario's fault into the shared eval world and resets it afterwards. It is an operation with a go behind it, never something CI runs.
