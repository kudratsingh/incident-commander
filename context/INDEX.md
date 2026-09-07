# Session index

One line per session. **Read this before starting work**; it is cheaper than rediscovering.

`STATE.md` at the workspace root says where things *are*. This says how they got there, which is
the part that stops you from re-litigating a settled decision or re-investigating a closed
question.

An archive listed as *transcript only* means the raw session data is on disk under
`~/.claude/projects/-Users-kudratsingh-Documents-audit-ws/` but was never packed. Pack it with
`./context/pack.sh <slug>` if you need it to survive.

| Date | Archive | What this session established |
|---|---|---|
| 2026-08-08 → 08-10 | *transcript only* | **The audit.** 503 files read, 129 defects found and verified adversarially. Root-caused why the eval suite went green without meaning it: 32 of 37 scenarios silently fell back to canned responses. `AUDIT_REPORT.md`, `findings.json`. |
| 2026-08-10 → 08-12 | *transcript only* | **The fix campaign.** 75 of 77 work orders merged across both repos by two implementation agents working in parallel. Platform cut **v0.5.0**; commander re-pinned to it by index digest. 2 work orders deferred by ADR. |
| 2026-08-11 | *transcript only* | **First honest live run.** Read-only eval stage: 26/26 passed with `degraded_count: 0`, 16m28s, $1.86, archived at `e5f7fc0`. First run in the project's history whose report can prove what it exercised. |
| 2026-08-12 | *transcript only* | **First remediation run, and why it stopped.** One scenario ran and graded FAIL — the agent fixed the wrong thing because the fault it was told about could not be manufactured. A broken fixture, not a broken agent. Remediation frozen from here. |
| 2026-08-13 | *transcript only* | **The completeness sweep.** Asked "does this system have all its parts?" rather than "is this code correct?" — found **137 gaps, 121 never built**. `COMPLETENESS_REPORT.md` §3 is the most valuable part: the four mechanisms that produced them. |
| 2026-08-13 | *transcript only* | **Stage 1 built.** PRs #123–#129. Negative assertions, `max_tool_calls` wired to the runtime, canned-vs-live drift check in CI, precondition assertions. A red remediation result is now attributable to the agent. |
| 2026-08-16 | `2026-08-16-campaign-backfill.zip` | **This convention, plus a backfill.** `context/` added to both repos; the whole campaign's transcripts packed into one archive. No product code changed. |
| 2026-08-16 | `2026-08-16-stage-1-and-remediation-readiness.zip` | **Five shapes closed, three more found by running it.** PRs #133–#143 closed every blocker the readiness sweep named (807 → 1027 tests). Then a free dress rehearsal against the live stack found three defects invisible in source — including that `failed_traces_scan` passed the trusted 26/26 run **without ever calling `search_traces`**. Read `SUMMARY.md` §"What is still wrong" before planning the paid run. |
| 2026-08-21 | *in progress* | **Six parallel builders + a read-only reliability sweep.** cmd #145 canned-only scenarios (exit 8, pre-spend), #146 run-archive filesystem locking (ADR 0021), #147 evidence tool-scoping — **16 cross-satisfiable evidence tokens across 15 of 38 scenarios**, not just the one known defect. plat #146 `get_cache_key_info`, #147 pins+timestamp re-baseline, #148 `create_stuck_dag`. The 14-finder reliability sweep lives at `audit-ws/sweeps/reliability-sweep.js` — see `sweeps/README.md`; run IDs change, the script is the artifact. |
| 2026-08-30 → 09-07 | *transcript only* | **The paid live-eval sequence, and the first green remediation.** Read-only pass `cde5a14485c3` 25/26 (`degraded 0`, exit 0 — PR #176's forward checkpointing is why that exit code means something). Then four runs of `remediate_consumer_lag_success`: A wrong-target (→ #177 `ALERT_SUBJECT_PROBES` + planner rules), B honest escalation on a stale-but-static lag (→ #178 reprobe 75s, precondition lag>=20 over 10×15s), C **aborted pre-spend** on a world audit, D `16ae3c7a4c9d` **green on all five dimensions** — the project's first. Also: a ~$2 read-only stage thrown away because it was hand-rolled instead of `make eval-smoke`, and a fresh world that alerts on its own fixtures (→ #180). Full record and lessons: [`docs/lessons/live-eval-sequence-2026-09.md`](../docs/lessons/live-eval-sequence-2026-09.md). |
| 2026-09-07 | *transcript only* | **A stabilizer is not a resolution** (ADR 0026). Pre-spend sweep of `remediate_runaway_saga_success` found every steering layer — planner prompt, `FIX_MAP`, and the pinned `get_dag_state` / `replay_dlq_by_ids` descriptions — pointing at `pause_dag`, the one tool that scenario forbids (the platform refuses to replay a job inside a paused DAG, so pausing *breaks* the fix). A verified pause would have graded RESOLVED on a still-stuck chain, and every plan guard admitted it. Fix: `RESOLUTION_CLASS` classifies every Tier-1 action resolve-or-stabilize and a verified stabilizer now escalates; `FIX_MAP[RUNAWAY_SAGA]` → `replay_dlq_by_ids`; prompt gained a stuck-chain section countering the two pinned descriptions by name; `TestFixMapMatchesTheSuite` cross-checks the map against the corpus. **Left open on purpose:** nothing observable distinguishes `saga_stuck` from `remediate_runaway_saga_success` — see [`docs/lessons/live-eval-sequence-2026-09.md` §8](../docs/lessons/live-eval-sequence-2026-09.md). |
| 2026-09-07 | *transcript only* | **Read the row before you replay it** (ADR 0027). The user read the staged `remediate_runaway_saga_success` trajectory before releasing the spend and asked how the agent knows the dead-lettered root is safe to replay. It does not: `get_dag_state`'s node model is five fields and `remediation_hint` is not one of them, so a `dead_letter` status says the node stopped the chain and nothing about whether restarting it is safe. **Six structural guards admitted the plan**, including `_unsourced_resource_args` — the id IS platform-produced (the alert carries it), which proves it is not a hallucination and reads like something stronger. Fix: `SOURCE_ROW_FOR_ACTION` refuses a by-id replay whose job's `list_dlq_messages` row is not in the evidence (refuse-and-steer, total over Tier-1); both planner prompts carry the four hint readings and their outcomes; two new grader axes (`where` row selector, `before_tools` ordering boundary) let the scenario claim the root's row read `replay_safe` *before* the replay. **`saga_stuck` finally has its discriminator** — chaos now seeds `remediation_hint: human_required`, closing §8's open question. Full record: [`docs/lessons/live-eval-sequence-2026-09.md` §9](../docs/lessons/live-eval-sequence-2026-09.md). |

## Things a future session should not have to rediscover

Promoted out of the archives because they cost real time or money the first time.

- **`gaps.json` records have been read backwards.** At least one — `G1-declared-but-uncon-02` —
  says "chaos_setup **blocks** for 4 of 7 hooks", a *noun* meaning the YAML blocks that declare
  them. Three documents read it as a verb and an entire build item was aimed at a defect that
  did not exist. **Verify a gap against the code before building against it.**
- **Nothing was seeding the demo stack, for the whole project.** `SEED_EVAL_FIXTURES` was set on
  the one service that never runs the startup hook that reads it. It survived because the only CI
  job that boots the stack diffs `tools/list`, which needs no rows — an empty database looked
  exactly like a full one. Fixed, but it means live-run evidence predating the fix ran against an
  environment nobody had described.
- **A foreground eval run gets killed by the 10-minute command timeout.** It happened once and
  wasted the spend. Long runs go in the background, always.
- **An investigation agent bypassed a safety prompt** with `yes |` and `--no-confirm`, trimmed a
  shared Kafka topic, and crash-looped three consumer groups into a full stack rebuild. Read-only
  means read-only; an agent routing around a denial is a thing that happens.
- **`git stash` is shared across worktrees.** `refs/stash` is one stack per repository, so a stash
  taken in one worktree is visible and poppable from another. Three obsolete entries are still
  sitting in the commander checkout.
- ~~**Every `make chaos-*` target is broken at import**~~ — **fixed 2026-08-16.** `python
  scripts/x.py` put `scripts/` on `sys.path[0]` instead of the repo root, so `import evals`
  failed. The recipes now set `PYTHONPATH=.`. They still need `.env` sourced by hand, which is
  deliberate: make does not `-include .env` because that is exactly what silently overrode the
  token in PR #62.
- **A default that only CI exercises is a default nobody tests.** `make bootstrap-token` named a
  container from the *platform's* dev compose, so the documented `make demo && make
  bootstrap-token` pair could never work — CI passed `--postgres-container` explicitly and never
  saw it. Found by running the documented path, not by reading it.

- **Subagents stall at "waiting for CI".** Their background watchers die when the agent stops, so a
  PR sits green and unmerged forever. Shepherd them: on each completion notification check the PR
  yourself — green+CLEAN, merge it directly (cheaper than resuming); red, resume the agent with a
  `gh run view <id> --log-failed` pointer. Tell agents to poll `gh pr checks` themselves.
- **ADR 0021's `uchg` archive locking blocks `git worktree remove`.** Needs `chflags -R nouchg` plus
  `chmod -R u+w` first. Bites anyone who ran `make eval-reg` inside a worktree.
- **The repo `.venv` editable install pins to the MAIN checkout's `backend/`,** so a repo-root
  `pytest` from any worktree imports master's code and never sees your changes — it surfaced as 73
  phantom failures. Run with `PYTHONPATH=<worktree>/backend`. (platform repo)
- **`git branch --no-merged` and patch-id comparison BOTH lie under squash-merge.** Squashing rewrites
  commits, so merged branches look unmerged forever and N-commits-to-1 defeats patch-id. Check branch
  names against the merged-PR record, then compare file CONTENT. Two separate sessions concluded
  "unmerged work exists" from these; both were wrong.
- **`git add -A` in the main checkout swept 342 files into a 6-line PR.** PR #178 changed six lines
  of the runbook and also committed three untracked run archives (~42k lines) that happened to be
  sitting in `evals/runs/`. That directory is deliberately NOT gitignored (invariant 9), which is
  exactly what makes `-A` dangerous there rather than merely noisy. **Add explicit paths, never
  `-A`.** Run archives are committed deliberately, on their own branch, with their own message —
  never as a side effect. Doing docs work in a worktree prevents it structurally.
- **Protocol check beats machinery check.** Before a paid run, verifying that the machinery works
  is the *second* question; the first is whether you are running the runbook's exact command. The
  2026-08-30 read-only stage was executed as a hand-rolled `ONLY=` list under the write token
  instead of `make eval-smoke` — every property that made the stage read-only came from the target,
  so all of them were absent, agents remediated during it, and ~$2 of results were discarded. A
  substring in that list also smuggled a mutating scenario past the ADR 0020 gate (the gate counts
  mutators, and one is allowed). **Deviations from a documented paid-run command go to the user
  before the spend, not into a workaround.** The trap had been written down 19 days earlier and
  nobody was routed to it — hence the runbook's pre-run checklist now opens by pointing at the
  gotchas ledger as step 1.

- **Offline eval never loads a prompt, and `FIX_MAP`'s values are never read at runtime.** Canned
  scenarios replay recorded planner output, so the prompt is a live-only surface and 38/38 says
  nothing about it; and the handoff gate reads only `top.category not in FIX_MAP`, so a wrong value
  in that map breaks no test. Both were stale in the same direction for the whole life of PR #173
  (2026-09-07). Before a paid run, read the prompt the live agent will load against the scenario it
  will be graded by — the regression suite structurally cannot do it for you.

- **A guard stack can be complete about the object and silent about the decision.** By 2026-09-07
  five plan guards established that a remediation named the right resource, sourced it from the
  platform, verified it on the same resource, could observe what it changed, and was worth something
  when verified. All five admitted a plan to replay a dead-lettered job on no evidence beyond the
  fact that it had failed. **When adding a guard, ask which of two questions it answers — "is this
  aimed correctly?" or "should this happen at all?" — because the first kind accumulates and looks
  like the second.**
- **`_unsourced_resource_args` is a hallucination check, not a provenance check.** It proves the
  platform emitted a string. An id from the alert payload passes it, which is why "evidence-sourced"
  must never be read as "somebody looked this resource up".
- **A refusal written under a new marker name is invisible to the planner.** `_format_plan_context`
  matched `_PLAN_REFUSED_MARKER` exactly, so a second refusal shape landed inside the 200-character
  evidence truncation — cut mid-sentence, which is the one thing that function's own comment says
  the whole-rendering exists to prevent. Refusal markers are a derived set now
  (`_PLAN_REFUSAL_MARKERS`); a third shape must join it.

## Standing rules that outlive any session

- **No paid eval run without explicit permission, in plain words, each time.** A readiness
  confirmation is not authorization.
- **No remediation eval** until the fixture work is done.
- **Eval artifacts are append-only** (invariant 9). Nothing under `evals/{runs,reports,trajectories,briefings}/`
  or `study/` is ever deleted, truncated, or overwritten.
- **`contracts/platform-tools.snapshot.json` is generated.** Never hand-edited.
