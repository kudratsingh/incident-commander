# Running a fix campaign with parallel agents — what went wrong

Case study from the 2026-08 audit remediation campaign, where several agents worked this
repo at the same time to land 31 work orders in about six hours. Two problems cost real
time and nearly cost real work. Both are invisible until they bite, and neither is
specific to this repo. Written after the campaign closed at `0272be8`.

Read this before setting up any job where more than one agent shares a checkout.

## 1. `git stash` is shared across worktrees, and agents ate each other's work

**What we were doing.** Each agent worked in its own git worktree off the same repository.
The agent protocol asked every work order to prove its new test fails before the fix is
applied — the "fail at HEAD" standard. The obvious way to do that is to stash your changes,
run the test, and pop them back:

```bash
git stash          # set the fix aside
pytest tests/unit/test_thing.py   # prove it fails at HEAD
git stash pop      # bring the fix back
```

**What happened.** Agents running at the same time popped each other's changes. One agent
lost its entire working tree and got it back only by finding the dangling commit in the
object store.

**Why.** `refs/stash` is a single stack for the whole repository. It is **not** per
worktree. Every worktree pushes onto and pops off the same pile, so `git stash pop` takes
whatever landed on top — which may be another agent's work, saved seconds earlier.

This is easy to miss because worktrees isolate almost everything else. Separate working
directories, separate `HEAD`, separate index. The stash looks like it should follow the
same rule. It does not.

**What we changed.** `git stash` is banned outright in agent protocols for this repo. The
fail-at-HEAD proof uses a patch file that never leaves its own worktree:

```bash
git diff > /tmp/fix-$$.patch          # save the fix, worktree-local
git apply -R /tmp/fix-$$.patch        # revert it
pytest tests/unit/test_thing.py       # prove it fails at HEAD
git apply /tmp/fix-$$.patch           # put it back
```

Copying the file aside, or running the test against a scratch checkout, work equally well.
The rule is only that nothing touches repository-wide state.

**The rule.** *When several agents share one repository, no agent may use a command that
writes to repository-wide state.* `git stash` is the one that bit us. Anything else stored
under `refs/` rather than per worktree deserves the same suspicion.

Put the ban in the protocol from the start. The failure is silent — nothing errors, the
wrong changes simply reappear — so by the time anyone notices, work is already gone.

## 2. Required status checks put a ceiling on useful parallelism

**What we were doing.** Eight agents on this repo at once, each taking a work order,
opening a PR, and merging when green.

**What happened.** `main` requires status checks to be up to date before a merge. So every
merge made every other open PR stale. With N agents, a single merge costs N−1 rebases, and
each rebase means pushing again and waiting for CI again.

One work order of roughly 80 changed lines needed **five rebases and took 2.7 hours**. The
change itself was maybe fifteen minutes of work.

**The arithmetic.** Adding agents adds rebase work faster than it adds throughput. Past a
handful, more agents make the campaign slower. We cut to two or three and finished faster.

**The natural comparison.** The sibling `incident-platform` repo ran roughly ten agents
over the same campaign without this problem, because its default branch has no required
status checks — nothing to go stale, nothing to rebase. Same task, same tooling, opposite
result, and the only difference is branch protection. That is as close to a controlled
comparison as this kind of thing gets.

**The rule.** *Check branch protection before choosing how many agents to run.*

- Default branch requires up-to-date status checks → **2-3 agents per repo**.
- No required checks → parallelism is limited by something else, run more.
- Across two separate repos → full parallelism is always fine; they never contend.

Two things reduce the cost further, whatever the setting: split work so agents touch
different files, since file-level overlap turns an automatic rebase into a manual merge;
and prefer fewer, larger, coherent PRs, since every open PR is one more rebase target.

## What to check before the next campaign

1. Is `git stash` banned in the agent protocol? Is a worktree-local alternative written out?
2. What does branch protection say, and how many agents does that allow?
3. Do the work orders overlap on files? Re-group them if so.
4. Is anything else in the protocol touching repository-wide state?

## See also

- `incident-platform/docs/lessons/parallel-agent-campaigns.md` — the same campaign from the
  other repo, including why an unprotected branch traded this problem for a different one.
- [`phase-6-hardening.md`](phase-6-hardening.md) — the earlier "prefer the structural fix"
  case study.
