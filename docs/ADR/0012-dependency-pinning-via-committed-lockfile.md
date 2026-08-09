# ADR 0012: Dependency pinning via the committed uv.lock

* Status: accepted
* Date: 2026-08-09
* Decider: Kudrat Singh

## Context and problem statement

CLAUDE.md promises "uv-managed, pinned dependencies", but no pinning mechanism existed: `uv.lock` was gitignored and untracked, `pyproject.toml` declares only `>=` floors, and all CI jobs installed with an unfrozen `uv sync --all-groups`. Every fresh clone and every CI run therefore resolved the newest allowed versions, so an upstream `anthropic`/`pydantic` release could flip the suite red — or silently shift agent and judge behavior — with zero repo diff (finding C-03). What is the single mechanism that pins dependencies, and how do upgrades happen?

## Decision drivers

* Reproducibility is the eval methodology's foundation: agent/judge behavior must not drift with upstream release cadence while the repo is unchanged.
* One mechanism, one place: the pin should not be duplicated across `pyproject.toml` and a lockfile.
* An edited-`pyproject`-but-forgot-to-relock PR must fail loudly in CI, not install stale pins silently.
* Local development should stay frictionless: editing `pyproject.toml` should auto-relock, not error.

## Considered options

1. Commit `uv.lock` as the single pin; keep `>=` floors; CI installs `--locked` (chosen).
2. Compatible-release (`~=`) pins in `pyproject.toml`.
3. Commit the lock but have CI install with `--frozen`.

## Decision outcome

Option 1:

* The committed `uv.lock` is the single dependency-pinning mechanism. The lockfile committed with this ADR is the existing one the currently-green suite ran against (`uv lock --check` clean at HEAD; 57 packages; e.g. `anthropic` resolved at 0.121.0 above its 0.117.0 floor) — deliberately not regenerated, so the pinned set is the one already validated.
* `pyproject.toml` keeps its `>=` floors as minimum-supported-API documentation; floors are never tightened to express pins.
* Every CI install runs `uv sync --locked --all-groups`. `--locked` installs strictly from the lockfile **and** errors when `uv.lock` is stale relative to `pyproject.toml`, forcing the re-lock into the same PR that changed the dependency declaration.
* Dependency upgrades are deliberate commits: `uv lock --upgrade` (or `--upgrade-package <name>`) run locally, validated by the full suite, and reviewed as an explicit lockfile diff. Upstream releases alone can no longer change what CI installs.
* The Makefile `setup` target stays a plain `uv sync --all-groups`: locally you *want* auto-relock when editing `pyproject.toml`; only CI must refuse to drift.

### Why the alternatives lose

**`~=` pins in `pyproject.toml`.** Duplicates the pin in two places (the lock still exists and still wins), churns `pyproject.toml` on every upgrade, and conflates "minimum API we support" with "exact version we run".

**`--frozen` in CI.** Also installs strictly from the lockfile, but *silently* installs the stale lock when `pyproject.toml` changed — exactly the forgot-to-relock failure `--locked` exists to catch.

### Consequences

* `tests/unit/test_dependency_pinning.py` pins the mechanism itself: `uv.lock` tracked and not gitignored, and every `uv sync` line in `.github/workflows/*.yml` carries `--locked`.
* A PR touching `pyproject.toml` without re-locking now fails all CI jobs at the install step by design.
* Lockfile diffs are large and generated; reviewers read them as "which versions moved", not line by line.

## More information

Finding C-03 (audit, High). This decision was earmarked as ADR 0011 in the fix-campaign work orders; 0011 was taken by the campaign eval-freeze ADR, so it lands here as 0012.
