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

## Standing rules that outlive any session

- **No paid eval run without explicit permission, in plain words, each time.** A readiness
  confirmation is not authorization.
- **No remediation eval** until the fixture work is done.
- **Eval artifacts are append-only** (invariant 9). Nothing under `evals/{runs,reports,trajectories,briefings}/`
  or `study/` is ever deleted, truncated, or overwritten.
- **`contracts/platform-tools.snapshot.json` is generated.** Never hand-edited.
