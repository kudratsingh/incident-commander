# The recording

The owner records the live demo and the one paid take is theirs. This folder is where the
recording and its stills go; nothing is committed here yet.

Expected contents, once recorded:

- `consumer-outage.<date>.mp4` — a consumer stops, the backlog climbs, the agent restarts the
  group and watches it drain.
- `dlq-backlog.<date>.mp4` — one replayable row, one poisoned row, and a briefing that names
  the row the agent deliberately left alone.
- Stills of the moments worth a screenshot: the phase strip mid-run, the hypothesis card with
  its confidence bar, and the briefing.

How to produce one: `docs/demo-runbook.md` — the checklist, what to say per phase, the
measured timings, and the paid-take protocol.

**A video file is large and this repo is not an asset store.** Before committing one, decide
where it belongs: a short clip beside the README is reasonable; a long capture belongs in the
private workspace (`audit-ws`) or in a release asset, with a link from here. Whatever is
decided, nothing worth keeping stays only on a laptop.
