# Architecture principles

Rules that hold across future work. Written after Phase 6 hardening ([lessons doc](lessons/phase-6-hardening.md)) codified what "structural fix" means in practice.

Every rule here has a specific bug that would have been prevented if the rule had been in place from Phase 0. This is the debt we already paid — don't pay it again.

## 1. Structural over prose

**Rule.** If an LLM output field can be constrained by the JSON schema on the `tool_choice=record_output` tool, constrain it there. Prompts telling the LLM to "please use X" are documentation, not enforcement.

**Concrete guidance:**
- Value from a fixed set → `Literal` or `StrEnum`
- Discriminated union of shapes → Pydantic discriminator
- Numeric bounds → `Field(ge=..., le=...)`
- String bounds → `Field(min_length=..., max_length=...)`
- Structural constraint across fields → Pydantic `model_validator`
- Value is free-form for downstream human reading → keep as `str`, but note it in the docstring

**What triggered this rule.** `Hypothesis.name: str` in Phase 2. See [phase-6-hardening lessons](lessons/phase-6-hardening.md) for the full story.

**Related.** [ADR-0005](ADR/0005-hypothesis-and-action-schema-tightening.md).

## 2. Single source of truth for mappings

**Rule.** When code AND a prompt both refer to a mapping (category → tool, hypothesis → fix, error code → action), store it in one Python object and read from both places. Not two documents you have to keep in sync.

**Concrete guidance:**
- Fix mappings live in code (e.g. `FIX_MAP` in `investigation.py`)
- Prompt describes the mapping *from* the code (either by listing enum values or by loading `FIX_MAP.keys()` at prompt-render time)
- Adding a mapping is a one-line coordinated change: enum entry + `FIX_MAP` entry + prompt example. Test asserts the three are in sync.

**What triggered this rule.** The Phase 2 "hypothesis-name-to-fix-tool" mapping lived in the remediation prompt AND was assumed by the investigation planner's soft-string match AND was validated by runtime checks in `remediation.py`. Three places to keep in sync. When one drifted, the loop broke silently.

## 3. Default to the structural fix, not the band-aid

**Rule.** When you spot a symptom, the first proposal in a PR discussion should be the schema/type-system change that prevents the *class* of bug. Prompt tweaks and config changes go last, with the trade-off called out ("this fixes today's case but doesn't prevent the next one").

**Concrete guidance:**
- Bug reveals a free-form field the LLM misused → constrain the field structurally, don't tighten the prompt to "please don't do that"
- Bug reveals runtime code catching what should have been schema-caught → move the check into the schema
- Bug reveals a mapping drift → consolidate the mapping into one Python source (rule #2)
- Only fall back to prompt tuning when: (a) the field genuinely needs to be free-form, or (b) the structural fix is out of scope for the current PR AND you file the follow-up before merging

**What triggered this rule.** Phase 6 live-eval failure. First recommendation from me was a prompt tweak. User pushed back: "shouldn't we be doing the structural fix?" We were. The band-aid would have shipped tech debt.

**Codified in memory.** `feedback_default_to_structural_fixes` — my per-project rule to lead with structural proposals.

## 4. Evals surface design assumptions, not just bugs

**Rule.** When live-eval reveals a "bug" that turns out to be an LLM being reasonable but the code being too rigid, treat that as a design signal. The bug isn't in the LLM; the design assumed something that isn't true.

**Concrete guidance:**
- Don't rush to "fix" the LLM's behavior by tightening the prompt. First ask: was the LLM actually wrong, or was the design's assumption wrong?
- Document what the eval revealed in `docs/eval-methodology.md` case-study format. Future PRs read this to avoid repeating the mistake.
- Live-eval findings often expose Phase-N-shortcuts. Treat them as opportunities to tighten schemas, not as one-off correctness patches.

**What triggered this rule.** DLQ case study — the LLM correctly read `error_message: ValueError: invalid literal for int() at row 15,382` and classified it as "not replayable." Our scenario expected `replay_dlq_messages` to fire. The LLM wasn't wrong; the scenario assumption was.

**Related.** [ADR-0004](ADR/0004-eval-first-development-and-regression-gating.md), [docs/eval-methodology.md#case-study](eval-methodology.md#case-study-dlq-categorization-discovery).

## 5. Defense-in-depth for LLM-driven boundaries

**Rule.** Every boundary the LLM's output crosses should have two independent checks: schema (Pydantic + `tool_choice=record_output`) and runtime (code that reads the parsed output).

**Concrete guidance:**
- Schema check is fast and prevents the class of bug from being possible in production
- Runtime check catches drift (e.g., registry updated but Literal not regenerated) + gives clearer error messages during eval
- Don't remove the runtime check just because the schema now enforces the same thing. They protect against different failure modes.

**What triggered this rule.** After PR #43 added `Literal` tool names, the runtime `if tool_name not in TOOL_REGISTRY: escalate` was arguably redundant. We kept it because if the registry gets a tool added at runtime (via a plugin, or during migration between versions), the pre-import-frozen Literal won't include it — the runtime check would catch that gap.

## 6. Ship live-eval as early as the platform permits

**Rule.** Live-eval isn't a Phase-N-plus-later concern. It's the ONE gate that runs against a system that doesn't know the right answer. Ship it as early as the underlying platform can serve real traffic. Defer only for genuine platform-readiness reasons, never for "the offline path is so consistent."

**Concrete guidance:**
- If offline eval is 100% green for two straight PRs, that's a signal to run live, not a signal that live isn't needed
- Live-eval spend is worth more than compute — the bugs it surfaces are structural, not superficial
- Budget: $0.30–$1.50 per full run for a 30-scenario suite today. Below the cost of any bug it would surface.

**What triggered this rule.** Phase 2 shipped July 15. First live-eval July 30. In those 15 days, the free-form-name bug was invisible to every offline test. Shipping live-eval two weeks earlier would have caught it two weeks earlier.

## 7. ADRs for anything that constrains future work

**Rule.** Any decision that a future contributor could reasonably second-guess needs an ADR. Especially:

- New schema on a boundary (adds an enum, adds a Literal, adds a discriminator)
- Tier / privilege classification (what may/may not run auto-remediation, what needs approval)
- Framework choice (e.g. why hand-rolled state machine vs LangGraph — ADR-0002)
- Eval methodology change (grading dimensions, baseline gating rules)
- Anything that changes how PRs get reviewed (e.g. eval-first — ADR-0004)

**Concrete guidance:**
- One ADR per decision, in `docs/ADR/NNNN-<slug>.md`
- MADR format: context, decision drivers, considered options, decision outcome, why alternatives lose, consequences (positive + negative), revisit trigger
- Never rewrite an accepted ADR. Supersede it with a new one that references the old.

**What triggered this rule.** Phase 2 didn't write an ADR for the free-form `Hypothesis.name` decision. It was a "quick shortcut." Four months later, when the bug surfaced, there was no record of *why* it was a shortcut — just the shortcut itself. That made the "should we fix this structurally?" conversation harder than it should have been.

## When you're about to violate one of these

Add a line to the PR description acknowledging it + the trade-off:

> This PR intentionally violates principle #3 (structural over band-aid) because the structural fix requires a platform-side coordination that's out of scope. Follow-up filed as issue #NNN.

That's fine. The rules aren't laws — they're the defaults. Violating them with a reason on the PR is much cheaper than violating them silently and paying the debt later.

## Related documents

- [CLAUDE.md](../CLAUDE.md) — project constitution + invariants
- [docs/lessons/phase-6-hardening.md](lessons/phase-6-hardening.md) — the case study that produced these rules
- [docs/ADR/](ADR/) — accepted decisions
- [docs/eval-methodology.md](eval-methodology.md) — how evals discover the bugs these rules prevent
- [docs/safety-model.md](safety-model.md) — the safety invariants these rules operate within
