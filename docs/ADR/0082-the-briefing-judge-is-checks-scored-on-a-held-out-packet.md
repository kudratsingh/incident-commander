# ADR 0082: The briefing judge's rubric is a list of named checks, and its agreement counts only on a packet it was not written from

* Status: accepted
* Date: 2026-10-11
* Decider: Kudrat Singh (owner decision O-58, WO-R3-378, INC-010)
* Amends: [ADR 0052](0052-a-judge-number-is-withheld-until-its-judge-is-calibrated.md) § 5 ("a
  rubric edit lands one line at a time") for one case only: a rewrite the owner orders as one
  change, scored on a held-out label packet. Everything else in 0052 stands — the register, the
  withheld sentence, self-agreement at N=5, no temperature.
* Related: [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md) (the
  shared reading rules, kept word for word), INC-002 and INC-010 in `audit-ws/context/INCIDENTS.md`,
  WO-R3-379 (label packet 2 and the `packet` field on label rows), WO-R3-278 (the label leg)

## Context and problem statement

The label leg ran once on the owner's 16 hand labels (report `99181bf074ec`, 2026-10-11,
`claude-haiku-4-5`): the briefing judge agreed with the owner on **8 of 16**, and it was wrong in
both directions (INC-010).

* **Judge useful, owner not (3).** A guess promoted to a finding
  (`648a32f2339d:remediate_stale_cache_success`, "90 bytes … suggesting it may be nearly empty"); a
  recommendation lifted from a standing dead-letter row's own triage text
  (`0db6fe722f7c:deploy_correlation`); and `6e2def805db4:saga_stuck`, which states "the DLQ entries
  are all marked replay-safe" when two of three are not, never reconciles a stalled-saga page with a
  chain that reads completed, and recommends the standing rows' own fixes. The judge gave them
  0.85–0.95 groundedness. The owner's third pattern, a reading that cannot see the alerted signal
  reported as evidence the alert was wrong, is the reason on `0db6fe722f7c:postgres_slow`, which
  the judge called not useful only by marking its actionability down — its groundedness was 1.0.
* **Judge not useful, owner useful (5).** Every one scored actionability 0.5–0.6: briefings that say
  honestly "nothing was investigated, here is the one thing to look at" or "confirm the replayed job
  finished" lost half a point each for not naming a dashboard, a UI path or a query syntax.

The v1 rubric's groundedness and actionability were each a three-rung ladder of adjectives
("mostly grounded but with minor speculation", "directional but vague"). Nothing in it named the
three fault patterns, and its actionability ladder rewarded detail over correctness.

The briefing judge is an INSTRUMENT, not the quantity under study, so fixing its rubric is
legitimate — but the 16 labels are the whole set. A rubric rewritten from 16 labels and scored on
the same 16 measures how well it was fitted, not how good the judge is. The owner's order, O-58:
rewrite the rubric as checks from the three patterns plus the actionability fix (one PR, $0); draw a
second blind packet of 16 with three of the first 16 re-included unlabelled (WO-R3-379); the owner
labels it before the new rubric is run on it; score the new rubric on packet 2 ONLY, with
self-agreement at N=5, the first 16 shown as a dev set that does not count; if held-out agreement
clears the threshold the judge's numbers leave "measured", otherwise the finding is "the judge
cannot be rubric-fixed". Until then the judge stays registered as measured at 8/16
(`research_report.JUDGE_CALIBRATION_REPORTS`, unchanged by this record).

## Decision

### 1. Rubric v2 is five named checks, applied in order

`src/incident_commander/llm/prompts/briefing_judge.md`. Each dimension starts at 1.0; every check
that fires sets the ceiling it names; the score is the lowest ceiling any check set. Each check is
one sentence followed by one example from the owner's labels, quoted by briefing id with the label
the owner gave it. `reasoning` is one sentence per dimension naming the check that decided that
score by its bold name, or "no check fired" for a 1.0. The names are declared once,
`evals/graders/llm_judge.BRIEFING_JUDGE_CHECKS`, and a test holds the prompt to them in order.

| # | Check | Dimension | Ceiling | Owner's example |
|---|---|---|---|---|
| 1 | **invented fact** — a component, number or error the alert, run context and trail do not carry, or a claim that contradicts them | groundedness | 0.0 | `6e2def805db4:saga_stuck` (not_useful): "all marked replay-safe" while two of three rows read `wait_and_replay` |
| 2 | **derived, not copied** — the step offered for THIS incident is a standing dead-letter row's own `suggested_fix`/`triage` text (a row the alert does not name and the run did not act on) rather than what the incident's readings show; a standing row named as remaining, with what its own row says, after a derived step is the remainder rule, not a copy | both | 0.5 each | `0db6fe722f7c:deploy_correlation` (not_useful) |
| 3 | **blind probe** — a reading that cannot observe the alerted signal (a ping for a query-latency alert, a lag read for a queue-depth alert, a `total: 0` of a filtered slice) proves nothing about the alert; calling the alert false, transient or recovered, or the resource healthy, on it | groundedness | 0.5; 0.0 when that is the finding | `0db6fe722f7c:postgres_slow` (not_useful) |
| 4 | **a guess is not a finding** — a claim the trail supports only by inference (a size read as "empty"; a key name the run chose itself promoted to "the critical hot-key"), stated as fact or built on; a possibility marked as one and not acted on does not fire it | groundedness | 0.0 | `648a32f2339d:remediate_stale_cache_success` (not_useful) |
| 5 | **look at X, do nothing** — a recommendation naming the thing to look at, confirm or act on, where that thing is in the alert or trail, is complete ("nothing was investigated, look at this alert", "confirm the replayed job finished"); 0.5 only when a step has no target; 0.0 for no step at all; leaving out which dashboard, command or screen is never a deduction | actionability | 1.0 / 0.5 / 0.0 | `0db6fe722f7c:noise_low_severity` (useful) |

Check 1 is the v1 ladder's floor, kept as a check. The order lists four; without the floor nothing
would score a plain invented number, and three of the six existing traps (`bj-03`, `bj-04`,
`bj-06`) assert exactly that.

The reading rules stay as they were, word for word, ahead of the checks: the overclaimed verify
read, the fence, the chain node, the attribution block, the stalled chain, the remainder, and "the
same reading rule binds you". Five of them are ADR 0054 shared sentences (`{{rule:…}}`) that other
prompts carry too, so no other prompt's hash moves. `briefing_judge`'s served-text snapshot hash
moves once, alone (`27bffb14…` → `9f684d74…`); the calibration report's `rubric.sha256` moves
`6140102d…` → `20230ebc…` (27 → 39 lines) and the report's `rubric.checks` lists the five names.

The briefing WRITER is not changed. Two of the checks ("blind probe", "a guess is not a finding")
are rules about how evidence may be read, and INC-002's prevention clause says such a rule goes to
every reader. Giving them to the writer changes the agent under test — a prompt change gated by
`make eval-reg` and only validated by paid runs — which this order does not authorise; it is
recorded as follow-up work rather than done silently.

### 2. One trap per added check

Four `briefing_judge` traps (`evals/judge_calibration/traps.py`, ten in all), each built from the
SHAPE of an owner-labelled briefing with its own names, versions and ids, never its text — the
owner's reason is that briefing's label, and a test holds the longest run of characters a trap
shares with its source briefing under 40:

| Trap | Check | Asserts |
|---|---|---|
| `bj-07-recommendation-copied-from-a-standing-row` | derived, not copied | grounded=no actionable=no |
| `bj-08-blind-probe-called-a-false-positive` | blind probe | grounded=no actionable=yes |
| `bj-09-a-guess-stated-as-a-finding` | a guess is not a finding | grounded=no actionable=yes |
| `bj-10-nothing-investigated-look-at-the-alert` | look at X, do nothing | grounded=yes actionable=yes |

`traps.BRIEFING_CHECK_TRAPS` maps every check to its traps (check 1 to the existing `bj-03` and
`bj-06`). The set can fail the judge INC-010 measured: a scripted fake answering the four new traps
the way rubric v1 answered their shapes disagrees on all four (6/10), and one applying the checks
agrees on all ten. The six existing traps are byte-for-byte unchanged, so their history stays
comparable.

### 3. Agreement counts only on a named, held-out packet

Label rows carry the `packet` they were labelled in (WO-R3-379; a row without one is packet 1).
`make judge-calibration JUDGE=briefing_judge LABELS=1 PACKET=<p> DEV_PACKET=<d>`:

* **`PACKET=p` is the number that counts** — p's labels only, and only its briefings that carry no
  label in any other packet. A briefing labelled in another packet was in the rubric writer's view
  (O-58 puts three of packet 1 back into packet 2 for the owner's test-retest, and packet 1 is the
  dev set); it is listed under `not_counted`, with whether the rubric quotes it, and it is not asked.
* **The packet the rubric was written from cannot be named as held out.** If the rubric quotes a
  briefing that only packet p labels, p is the packet the rubric was written from, and `PACKET=p`
  is refused before any call: rubric v2 quotes five of packet 1's briefings, so `PACKET=1` is
  refused by the bytes of the prompt, not by anyone's memory of which packet came first.
* **`DEV_PACKET=d`** is scored beside it in its own block, `counted: false`, with its own stability.
* **Test-retest**: for every briefing p labels that another packet labelled too, the two labels,
  their dates and whether the owner agreed with themself — "a judge cannot be expected to agree with
  the owner more often than the owner agrees with themself".
* **Self-agreement at N=5** beside each number, and how many first answers' `reasoning` name a
  check (`llm_judge.cited_checks`), so a reader can see whether the judge followed the procedure.
* Each block records the first and last `labelled_at` of the labels it scored, so a reader can check
  the labels were in the file before the run (O-58 item 3).
* **A label file holding more than one packet is refused without `PACKET=`**, before any call:
  "every label in force" would put the dev set into the counted number. A file of one packet (today's)
  is scored whole, as before, with no threshold line. Every other refusal (an empty packet, a packet
  whose every briefing is a repeat, a dev packet equal to the held-out one, a dev packet with no
  held-out one) is also raised before the first call.

### 4. The threshold: `HELD_OUT_AGREEMENT_THRESHOLD = 0.8`, plus two conditions

`evals/judge_calibration/label_leg.py`. The harness says the judge CLEARS when all three hold:

1. held-out agreement ≥ 0.8 — on 13 briefings, 11 or more;
2. strictly more agreements than always answering the packet's majority label would get;
3. every counted briefing was answered (no judge error, no briefing missing from the checkout).

Why 0.8. Packet 1 was 10 useful / 6 not_useful, so a judge that says "useful" every time scores
≈ 0.62 on a packet of that mix. With 13 held-out briefings, the chance of clearing:

| True agreement | ≥ 10/13 (0.75) | ≥ 11/13 (0.8, chosen) | ≥ 12/13 (0.9) |
|---|---|---|---|
| 0.62 (majority label) | 21% | **8%** | 2% |
| 0.70 | 42% | 20% | 6% |
| 0.85 | 88% | **69%** | 40% |
| 0.90 | 97% | **87%** | 62% |

0.75 lets a judge no better than the majority label through one time in five; 0.9 fails a judge
that agrees with the owner 85% of the time more often than it passes it. 0.8 keeps the false clear
under one in ten and passes a genuinely good judge most of the time. Condition 2 guards a lopsided
packet (twelve useful of thirteen: "useful" every time scores 0.92 and proves nothing); condition 3
keeps a smaller denominator from reading as a pass.

A 95% interval was considered and refused: at n = 13 the Wilson lower bound for 11/13 is 0.58 and
for 12/13 is 0.67, so "lower bound above the majority baseline" means 12 of 13 — the 0.9 column.
The interval is the honest description of 13 labels; the threshold is a decision rule a person
reads beside it, and the owner's own test-retest (three repeats) is printed next to it.

**Reported, never acted on.** The harness prints CLEARS or does not clear and stops. Whether the
judge's numbers leave "measured" is the owner's decision (O-58 item 5);
`research_report.JUDGE_CALIBRATION_REPORTS` stays `99181bf074ec` (8/16) until the owner decides,
and no code reads the verdict.

### 5. One change, not one line at a time (amends ADR 0052 § 5)

ADR 0052 § 5 asks for one rubric line per commit, each with a rerun, so that a delta between two
calibrations is attributable. Here that would mean five paid reruns, each scored on the same 16
labels the lines were written from — five measurements of fit. The owner ordered the rewrite as one
change, and the held-out packet is its rerun: the hash moves once, every report records which rubric
bytes it measured, and the one number that counts is taken on labels the rewrite never saw. The
one-line rule stands for every other rubric edit.

## Consequences

Positive:

* The judge is told what the owner's labels say a useful briefing is, in checks it can cite, and
  the harness can count whether it cites them.
* The trap set can now fail the judge on each of INC-010's patterns, for free, every time the
  calibration runs.
* Held-out, dev and test-retest are three separate numbers that cannot be merged by accident.

Negative:

* The served prompt grows from 8,679 to 12,696 characters (≈ +1,000 input tokens a call). Mitigation:
  the paid scoring is costed with it (below).
* The judge's register still says 8/16 until a paid run on packet 2 and the owner's decision.
* Two checks are not yet given to the writer (§ 1) — a known half-rule, filed as follow-up.

The paid step, deferred and needing its own yes after the owner's packet-2 labels are imported:
`make judge-calibration JUDGE=briefing_judge LABELS=1 PACKET=2 DEV_PACKET=1 LIVE=1 YES_SPEND=1 WRITE=1`
— 13 held-out briefings + 16 dev briefings + 10 traps, five asks each, 195 calls, ≈ $0.30 at the
label leg's observed rate scaled for the longer prompt; ≈ $0.20 without `DEV_PACKET` (115 calls).

Revisit trigger: the held-out score does not clear (O-58: "the judge cannot be rubric-fixed"), or
the owner's test-retest is below the threshold itself, which would say the label is noisier than
the bar.

## More information

* `evals/graders/llm_judge.py` — `BRIEFING_JUDGE_CHECKS`, `cited_checks`
* `evals/judge_calibration/label_leg.py` — `HELD_OUT_AGREEMENT_THRESHOLD`, `held_out_verdict`,
  `labels_by_packet`, `owner_test_retest`
* `evals/judge_calibration/traps.py` — `bj-07` … `bj-10`, `BRIEFING_CHECK_TRAPS`
* `tests/unit/test_briefing_judge_checks.py`, `tests/unit/test_held_out_label_leg.py`
* `docs/eval-methodology.md` § Judge calibration, `docs/runbook.md` § Judge calibration: held-out scoring
