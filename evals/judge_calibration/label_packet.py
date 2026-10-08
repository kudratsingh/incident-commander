"""``make label-packet``: write the labelling packet, or import a filled one (WO-R3-278)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from evals.judge_calibration import labels


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.judge_calibration.label_packet",
        description="Write the briefing labelling packet, or import a filled one (WO-R3-278).",
    )
    parser.add_argument("--import", dest="packet", type=Path, help="a filled packet to import")
    args = parser.parse_args(argv)

    if args.packet is not None:
        try:
            result = labels.import_labels(
                labels.parse_packet(args.packet.read_text(encoding="utf-8"))
            )
        except labels.LabelError as err:
            print(f"refusing: {err}", file=sys.stderr)
            return 2
        print(f"appended {len(result.appended)} label(s) to {labels.LABELS_FILE}")
        for label_id in result.already_recorded:
            print(f"  already recorded, unchanged: {label_id}")
        for label_id in result.refused:
            print(
                f"  REFUSED {label_id}: already labelled differently; the file is append-only, "
                "so add 'SUPERSEDES: <id>' under the block to record a correction",
                file=sys.stderr,
            )
        return 1 if result.refused else 0

    today = datetime.now(UTC).date()
    picks = labels.select(labels.candidates(labels.committed_archives()))
    try:
        path = labels.write_packet(labels.render_packet(picks, on=today), on=today)
    except FileExistsError as err:
        print(
            f"refusing: today's packet already exists ({err.filename}); it is not replaced",
            file=sys.stderr,
        )
        return 2
    print(f"wrote {path}")
    for pick in picks:
        print(f"  {pick.candidate.id}  — {pick.why}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
