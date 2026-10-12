# ADR 0083: Two briefing faults are lookups, not judgments

* Status: accepted
* Date: 2026-10-12
* Decider: Kudrat Singh (owner decision O-61 item 2, WO-R3-382, INC-010)
* Related: [ADR 0082](0082-the-briefing-judge-is-checks-scored-on-a-held-out-packet.md) (the judge's
  rubric v2, which carried "derived, not copied" as a check and did not learn it),
  [ADR 0065](0065-a-briefing-names-its-remainder-in-a-slot-not-in-its-prose.md) (a remaining row is
  named, with what it needs), [ADR 0041](0041-read-the-whole-queue-before-you-replay-part-of-it.md) and
  [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md) (a platform hint
  stands unless a reading refutes it — so naming a row with its own hint is not a fault),
  [ADR 0038](0038-ground-truth-is-evaluator-only.md) (nothing here reaches the agent)

## Context and problem statement

The owner hand-labelled 29 briefings over two packets (INC-010). Two of the reasons they gave are
not matters of taste:

* **A cached reading called confirmed.** Packet-2 #15 (`0db6fe722f7c:multi_probe_billing`, not
  useful) says "billing-consumer lag is confirmed at 15,000" about a `get_consumer_lag` result with
  `source: static` and `measured_at: null`. Packet-1 #13 (`…:multi_probe_hypothesis_evolution`,
  useful) reads the same kind of reading and says it is "cached, timestamp unknown".
* **A recommendation copied from a standing dead-letter row.** Packet-1 #6, #9, #16 and packet-2
  #10 offer, as the step for THIS incident, the SMTP row's own `suggested_fix` ("check
  smtp.mailer.internal ECS task health … VPC egress rules") on pages that are about a deploy, an
  error rate or a stalled saga.

The briefing judge passed all of these, under rubric v1 (8/16) and rubric v2 (5/13 held out, #10
and #15 still passed; O-60). O-61: the judge stays registered at 8/16, and these two patterns
become deterministic checks — "lookups against the trail, not judgments".

## Decision

### 1. A new grade dimension, `BRIEFING`, appended to `GradeDimension`

`evals/graders/briefing_grader.py` holds both checks; `deterministic._grade_briefing` assembles the
verdict (the `root_cause.py` split). It reads only the briefing's own `investigation_trail` (the
trail the writer and the judge are shown), the alert and the run's actions — all in the archive.
It applies only when a writer filled `findings` or `recommendation`; otherwise it passes with
`no briefing prose set`, the "no … set" shape the regression gate reads as never measured. PASS
when neither check fires; FAIL names every reading-and-clause and every row-and-step that fired.
No model, no score, no threshold tuned at run time.

### 2. A cached reading called confirmed

* **Which readings.** `READING_FRESHNESS` names, per tool, the value, the subject, the `source`
  field and the timestamp field. A unit test derives the tool set from
  `contracts/platform-tools.snapshot.json` (a `measured_at` that may be null or absent, or a
  `source` that names a `live` value): today only `get_consumer_lag` (`lag`, `consumer_group`,
  `source`, `measured_at`). The SLO, outbox, control-loop and breaker reads declare `measured_at`
  required, so they are never untimestamped. A reading is cached when `source != "live"` or the
  timestamp is missing.
* **Which sentences.** `findings` and `recommendation`, cut into clauses at `;` and at a sentence
  stop. The escalation reason is the planner's text, not the writer's, and is not read.
* **The rule.** A clause fires when it carries the reading's value (`15000`, `15,000`, `15k`), names
  what was read (the value field or the subject), holds a freshness word (`confirmed`, `verified`,
  `current`, `currently`, `live`, `fresh`, `freshly`, `re-measured`, `remeasured`) not preceded by
  "not", "never" or "no longer", and carries no disclosure (`cached`, `static`, `stale`,
  `untimestamped`, `no timestamp`, `timestamp unknown`, `without a timestamp`, `recorded constant`,
  `not live`, `not a live`, `not re-measured`). The disclosure is the owner's own line between #13
  and #15: the harm is a reader who believes the number is current, and a clause that says it is
  cached has told them.

### 3. A recommendation step copied from a non-subject dead-letter row

* **Standing rows.** Every row a `list_dlq_messages` result in the trail carries with a
  `suggested_fix`. The owner's word is `suggested_fix`; the triage `summary` describes the failure
  rather than prescribing a step, and counting it reddened a canned run whose step described its
  own subject row in the words of a sibling row's summary.
* **The subject.** A row is this incident's subject when the alert names its id (full or the first
  8 characters), when the alert is a dead-letter page (`source: platform.dlq`) and the row is in
  the page's slice (the alert's `remediation_hint`, or the whole queue when it names none), or when
  the run acted on it: an action call, an attempted action a marker records, or the briefing's
  `attempted_action` names its id, or a `replay_dlq_by_category` names its hint.
* **A row's own words.** The `suggested_fix`'s words — lower-cased, ports stripped, function words
  and the generic imperatives (check, verify, confirm, review, look …) dropped — that appear
  nowhere else in the trail (the row's other sightings removed) and not in the alert. A word the
  incident's own readings carry (a deploy note's "billing", a version string) is derivable from the
  incident, so it is never evidence of a copy.
* **A copy.** A `recommendation` step (cut at `;`, a sentence stop or "then") that carries
  `COPY_WORD_THRESHOLD = 3` of a non-subject row's own words, unless the step names the row by id or
  names it as remaining (`remaining`, `remain(s)`, `unresolved`, `unrelated`, `not addressed`,
  `independent of this incident`). That exception is ADR 0065's remainder: a standing row named as
  left, with what its own row says, is the honest handoff the owner labelled useful (packet-1 #1,
  #4, #8; packet-2 #3, #8).
* **Why three words, not "eight consecutive words".** The writer paraphrases: the owner's four
  copies share at most five consecutive words with any row's fix, and #6 shares three. The
  threshold separates the owner's smallest copy (#6: `vpc`, `egress`, `smtp.mailer.internal`) from
  the largest coincidence in a useful briefing (#13: "check pod/task health" shares two, `task` and
  `health`). It is set from the labels; the labelled test is what would catch it drifting.

### 4. Readers

`report.dimensions` carries the row in every new report. The run summary prints
`briefing lookups: N/M written briefing(s) pass …` beside the root-cause line. `research_report`
adds `briefing_lookups` to an arm and a "briefing lookups" column to the leaderboard, and the
phase-close report a "Briefing" column on the live-leg table — each only when a row in scope
carries the dimension, so every committed document regenerates byte for byte.
`scripts/regrade_archive.py` already omits a dimension the archive was not graded on from the
row's detail; it now omits it from the row's verdict too, for the same reason — an archive graded
before BRIEFING has no verdict on it to change. The judge's prompt, rubric and calibration are not
touched.

## Consequences

* **Where it fires on the owner's labels** (`tests/unit/test_briefing_grader.py`): the cached check
  on packet-2 #15 only; the copied check on packet-1 #6, #9, #16 and packet-2 #10 (and packet-2
  #13, which is #9 shown again); neither on any briefing the owner called useful.
* **Where it does not, and why.** Packet-2 #15 offers "verify smtp.mailer.internal ECS task
  health" in a step that names "the remaining retryable DLQ entries"; the remainder exception reads
  that as naming the rows, so the copied check does not fire there (#15 fails on the cached check).
  The owner's reasons on packet-1 #9 and packet-2 #13 also say the static lag was "reported as a
  sustained live number"; the word there is "sustained", which is not in the freshness list, so the
  cached check does not fire on them (both fail on the copied check). Neither rule was widened to
  catch them.
* **Known over-reach, pinned.** The cached rule reads clauses, not grammar: "lag was 15,000 and the
  restart was confirmed" fires. The copied rule's remainder words are read anywhere in the step.
* **On the corpus.** Every canned run passes (65 written briefings, 2 with no prose). On committed
  live archives it fires on INC-001's briefing (`4974811d236f`, the SMTP row's fix after calling
  the remaining rows "cleared or addressed"); `tests/unit/test_verify_shapes.py` now excludes
  BRIEFING from its "every dimension passes" claim, which was about the verify shape, and pins the
  red instead.
* **A new FAIL fails the run.** `passed` is the conjunction, so a live run whose briefing commits
  either fault is red, with `failure_class` `unclassified` (no bucket was added). The baseline is
  not re-blessed: a dimension the baseline lacks is new coverage to the gate, not a regression.
* **What it does not do.** It does not judge whether a step is right, only whether it is a copy;
  it does not read a reading's freshness from prose; and it cannot see a copy that shares fewer than
  three of the row's own words.
