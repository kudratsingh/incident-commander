# Oracle gap — candidate_selector/best_of_n_enumerated/n=8 on claude-sonnet-4-6

WO-R3-347 pilot (WO-R3-375, owner O-55). Design: audit-ws docs/plans/research-buildout-v2.1/samples/oracle-gap.md § The pilot.
Study: oracle_gap_pilot — pooled_with_main: false. Its runs are reported on their own here and are never pooled into the main sample.
Archives: 476d64a5603a, 7467aae0f0ff, e64f4a237d1b (passes 0, 1, 2 of one repeated invocation).
Calibration report for this arm: 36e12b915900 — its id is printed beside every selector number below.

## Pooled (9 scored runs, 3 worlds)

| k | n | pass@k | selected@k | oracle gap | gap 95% interval (worlds resampled) |
|---|---|---|---|---|---|
| 1 | 9 | 8 (0.8889) | selected@1 = 3/9 (0.3333) [cal 36e12b915900] | oracle gap@1 = 5/9 (0.5556) [cal 36e12b915900] | gap interval@1 = [0.0, 1.0] [cal 36e12b915900] |
| 2 | 9 | 9 (1.0) | selected@2 = 3/9 (0.3333) [cal 36e12b915900] | oracle gap@2 = 6/9 (0.6667) [cal 36e12b915900] | gap interval@2 = [0.0, 1.0] [cal 36e12b915900] |
| 4 | 9 | 9 (1.0) | selected@4 = 3/9 (0.3333) [cal 36e12b915900] | oracle gap@4 = 6/9 (0.6667) [cal 36e12b915900] | gap interval@4 = [0.0, 1.0] [cal 36e12b915900] |
| 8 | 9 | 9 (1.0) | selected@8 = 3/9 (0.3333) [cal 36e12b915900] | oracle gap@8 = 6/9 (0.6667) [cal 36e12b915900] | gap interval@8 = [0.0, 1.0] [cal 36e12b915900] |

Every scored run at k=8, three ways — what the selector did with the set (O-57):

| n | picked correct | picked wrong | declined to pick (probe_more, escalate, none) |
|---|---|---|---|
| 9 | picked correct@8 = 3/9 [cal 36e12b915900] | picked wrong@8 = 3/9 [cal 36e12b915900] | declined to pick@8 = 3/9 (probe_more 0, escalate 3, none 0) [cal 36e12b915900] |

`none` under declined to pick counts a selector that ran on the deciding step and named no decision; every record the agent writes names one, so a count there means a malformed or foreign trace.

Runs that lost an available answer, by what the selector did at the deciding step:
gap by decision@8 = escalate 3, select 3 [cal 36e12b915900]

The selector's pick against the generator's own top candidate:
selector vs top candidate@8 = n 9, both right 3, only selector right 0, only top candidate right 5, both wrong 1 [cal 36e12b915900]

## Per world (k=8)

| scenario | recording | runs | n | pass@k | selected@k | oracle gap | picked correct | picked wrong | declined to pick (probe_more, escalate, none) | repeats agree on pass@k | repeats agree on selected@k |
|---|---|---|---|---|---|---|---|---|---|---|---|
| dlq_mislabeled_replay_safe | e5d407945572 | 3 | 3 | 3/3 | selected@8 = 0/3 [cal 36e12b915900] | oracle gap@8 = 3/3 [cal 36e12b915900] | picked correct@8 = 0/3 [cal 36e12b915900] | picked wrong@8 = 0/3 [cal 36e12b915900] | declined to pick@8 = 3/3 (probe_more 0, escalate 3, none 0) [cal 36e12b915900] | yes | selected@8 agree = yes [cal 36e12b915900] |
| jobs_not_progressing_outbox_stall | c7f4746c4a61 | 3 | 3 | 3/3 | selected@8 = 3/3 [cal 36e12b915900] | oracle gap@8 = 0/3 [cal 36e12b915900] | picked correct@8 = 3/3 [cal 36e12b915900] | picked wrong@8 = 0/3 [cal 36e12b915900] | declined to pick@8 = 0/3 (probe_more 0, escalate 0, none 0) [cal 36e12b915900] | yes | selected@8 agree = yes [cal 36e12b915900] |
| workflow_stuck_resolver_stall | 6f030e848a9d | 3 | 3 | 3/3 | selected@8 = 0/3 [cal 36e12b915900] | oracle gap@8 = 3/3 [cal 36e12b915900] | picked correct@8 = 0/3 [cal 36e12b915900] | picked wrong@8 = 3/3 [cal 36e12b915900] | declined to pick@8 = 0/3 (probe_more 0, escalate 0, none 0) [cal 36e12b915900] | yes | selected@8 agree = yes [cal 36e12b915900] |

## Per run (k=8)

| scenario | archive | repeat | pass@k | selected@k | oracle gap | selector decision |
|---|---|---|---|---|---|---|
| dlq_mislabeled_replay_safe | 476d64a5603a | 0 | hit | selected@8 = miss [cal 36e12b915900] | oracle gap@8 = 1 [cal 36e12b915900] | selector decision@8 = escalate [cal 36e12b915900] |
| dlq_mislabeled_replay_safe | 7467aae0f0ff | 1 | hit | selected@8 = miss [cal 36e12b915900] | oracle gap@8 = 1 [cal 36e12b915900] | selector decision@8 = escalate [cal 36e12b915900] |
| dlq_mislabeled_replay_safe | e64f4a237d1b | 2 | hit | selected@8 = miss [cal 36e12b915900] | oracle gap@8 = 1 [cal 36e12b915900] | selector decision@8 = escalate [cal 36e12b915900] |
| jobs_not_progressing_outbox_stall | 476d64a5603a | 0 | hit | selected@8 = hit [cal 36e12b915900] | oracle gap@8 = 0 [cal 36e12b915900] | selector decision@8 = select [cal 36e12b915900] |
| jobs_not_progressing_outbox_stall | 7467aae0f0ff | 1 | hit | selected@8 = hit [cal 36e12b915900] | oracle gap@8 = 0 [cal 36e12b915900] | selector decision@8 = select [cal 36e12b915900] |
| jobs_not_progressing_outbox_stall | e64f4a237d1b | 2 | hit | selected@8 = hit [cal 36e12b915900] | oracle gap@8 = 0 [cal 36e12b915900] | selector decision@8 = select [cal 36e12b915900] |
| workflow_stuck_resolver_stall | 476d64a5603a | 0 | hit | selected@8 = miss [cal 36e12b915900] | oracle gap@8 = 1 [cal 36e12b915900] | selector decision@8 = select [cal 36e12b915900] |
| workflow_stuck_resolver_stall | 7467aae0f0ff | 1 | hit | selected@8 = miss [cal 36e12b915900] | oracle gap@8 = 1 [cal 36e12b915900] | selector decision@8 = select [cal 36e12b915900] |
| workflow_stuck_resolver_stall | e64f4a237d1b | 2 | hit | selected@8 = miss [cal 36e12b915900] | oracle gap@8 = 1 [cal 36e12b915900] | selector decision@8 = select [cal 36e12b915900] |

## Spend: $2.851459 over 12 runs (median $0.198179, max $0.444946 per run)

## Excluded (3)

- 476d64a5603a api_latency_db_query: not comparable: the replay missed 1 call(s) and refused 0 (ADR 0047) — re-record the world wider
- 7467aae0f0ff api_latency_db_query: not comparable: the replay missed 1 call(s) and refused 0 (ADR 0047) — re-record the world wider
- e64f4a237d1b api_latency_db_query: not comparable: the replay missed 1 call(s) and refused 0 (ADR 0047) — re-record the world wider

## What this cannot claim

- One model (claude-sonnet-4-6), one arm (candidate_selector/best_of_n_enumerated/n=8), recorded mode only: diagnosis is graded; outcome, action and safety are not claims a replay can make (ADR 0047).
- n = 9 scored runs over 3 worlds — below plan 03 § 10's floor of ~100 paired trials. The interval resamples worlds, so it speaks for worlds like these, not for the whole corpus.
- Per-world rows are a handful of runs each: read them as descriptions, never as rates.
- pass@8 is close to its ceiling by construction when 8 candidates cover every cause a family can have; the gap is then mostly a statement about the selector.
- A gap counts every run whose correct candidate was available and not taken, including a selector that escalated or asked for more; the split by decision says which.
- The three-way split (picked correct / picked wrong / declined to pick) counts every scored run, not only the gap: a wrong pick or a decline with no correct candidate in the set is counted there and is not part of the gap.
- 3 run(s) are excluded and listed with the reason; none is counted as a miss.
- Repeats of one world are the same recorded world replayed again (only the model's own sampling differs): whether they agree is a description of a handful of runs, not a rate.
- This is the oracle_gap_pilot study (pooled_with_main: false): its runs are reported here on their own and are never pooled into the main sample's numbers.
