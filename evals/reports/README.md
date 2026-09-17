# What is in this folder

- `baseline.json` — the blessed result `make eval-reg` gates against. Committed, and the only file here CI names by path.
- `latest.json` — a report from before reports were versioned. Old evidence, kept where it is.
- `runs/<YYYY-MM>/report.<stamp>.<id>.json` — one aggregate report per eval run, filed by the month it ran in.
- `baseline/` — `make baseline-report` output, the JSON and the same document as Markdown. Committed.
- `phase-close/` — `make phase-close-report` output, same pair. Committed.
- `dossiers/<scenario>/` — `make world-dossier ONLY=<scenario>`: the free read of a fault world before a paid run.
- `human/<scenario>/` — the readable step-by-step trajectory of each run, rendered from `evals/traces/`. Start here to see what the agent did.
- `human/_superseded/<scenario>/` — earlier renders of a run that has a newer one. Kept, never deleted; just out of the way.
- Every name is `<stem>.<timestamp>.<id>.<ext>` and nothing is ever overwritten. Ask `evals/artifacts.py` for the newest one (`python -m evals.artifacts newest human <scenario>`), never `ls -t`.
- Nothing here is ever deleted or edited (CLAUDE.md invariant 9). The one thing that moves files is `scripts/migrate_reports_layout.py`, and it only moves them.
