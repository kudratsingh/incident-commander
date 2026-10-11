"""``make label-packet``: write a labelling packet, or import a filled one (WO-R3-278, WO-R3-379).

Packet 1 is the 16 the selection rule picks. Packet 2 onward is drawn blind: ``--repeats`` of
the briefings an earlier packet labelled come back unmarked among new picks, shuffled by
``--seed``, and which ones repeat is written only to ``--key-out``, outside the repo.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from evals.judge_calibration import labels


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.judge_calibration.label_packet",
        description="Write a briefing labelling packet, or import a filled one.",
    )
    parser.add_argument(
        "--import", dest="filled", type=Path, help="a filled packet to import; its header names it"
    )
    parser.add_argument(
        "--packet",
        type=int,
        help="the packet to draw (default 1); 2 or more skips every briefing already shown",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=0,
        help="how many briefings an earlier packet labelled come back, unmarked",
    )
    parser.add_argument(
        "--seed", type=int, help="seeds the repeats and the shuffle; recorded in the header"
    )
    parser.add_argument(
        "--key-out", type=Path, help="where the repeat key is written; outside this repo"
    )
    args = parser.parse_args(argv)

    if args.filled is not None:
        return _import(args.filled, packet=args.packet)
    try:
        return _draw(args.packet or labels.FIRST_PACKET, args.repeats, args.seed, args.key_out)
    except labels.LabelError as err:
        print(f"refusing: {err}", file=sys.stderr)
        return 2


def _draw(packet: int, repeats: int, seed: int | None, key_out: Path | None) -> int:
    today = datetime.now(UTC).date()
    on_disk = labels.packets_on_disk()
    if packet in on_disk:
        raise labels.LabelError(f"packet {packet} already exists; a packet is drawn once")
    if labels.packet_path(today).exists():
        raise labels.LabelError(
            f"today's packet already exists ({labels.packet_path(today)}); it is not replaced"
        )
    if packet == labels.FIRST_PACKET:
        return _draw_first(repeats, seed, key_out, today=today)
    if seed is None:
        raise labels.LabelError(
            f"packet {packet} needs --seed (SEED=): the draw and its key must reproduce"
        )
    if repeats and key_out is None:
        raise labels.LabelError(
            "--repeats needs --key-out (KEY_OUT=), a path outside this repo for the key"
        )
    if key_out is not None:
        labels.check_key_path(key_out)
    drawn = labels.draw_packet(
        labels.candidates(labels.committed_archives()),
        packet=packet,
        repeats=repeats,
        seed=seed,
        rows=labels.read_lines(),
        packets=on_disk,
    )
    text = labels.render_packet(drawn.picks, on=today, packet=packet, seed=seed)
    print(f"wrote {labels.write_packet(text, on=today)}")
    print(f"packet: {packet}")
    print(f"seed: {seed}")
    print(
        f"{len(drawn.picks)} briefings: {len(drawn.new)} new by the selection rule, "
        f"{len(drawn.repeats)} from an earlier packet, shuffled; none is marked"
    )
    if key_out is not None:
        key = labels.write_key(drawn.key(), key_out)
        print(f"wrote the repeat key to {key}; keep it sealed until the labels are imported")
    return 0


def _draw_first(repeats: int, seed: int | None, key_out: Path | None, *, today: date) -> int:
    if repeats or seed is not None or key_out is not None:
        raise labels.LabelError(
            "packet 1 has no earlier packet to repeat; draw packet 2 or later with --packet"
        )
    picks = labels.select(labels.candidates(labels.committed_archives()))
    print(f"wrote {labels.write_packet(labels.render_packet(picks, on=today), on=today)}")
    for pick in picks:
        print(f"  {pick.candidate.id}  — {pick.why}")
    return 0


def _import(filled: Path, *, packet: int | None) -> int:
    text = filled.read_text(encoding="utf-8")
    number = labels.packet_number(text)
    if packet is not None and packet != number:
        print(
            f"refusing: {filled} is packet {number} by its header; --packet says {packet}",
            file=sys.stderr,
        )
        return 2
    try:
        result = labels.import_labels(labels.parse_packet(text), packet=number)
    except labels.LabelError as err:
        print(f"refusing: {err}", file=sys.stderr)
        return 2
    print(f"appended {len(result.appended)} label(s) for packet {number} to {labels.LABELS_FILE}")
    for label_id in result.already_recorded:
        print(f"  already recorded, unchanged: {label_id}")
    for label_id in result.refused:
        print(
            f"  REFUSED {label_id}: already labelled differently in packet {number}; the file "
            "is append-only, so add 'SUPERSEDES: <id>' under the block to record a correction",
            file=sys.stderr,
        )
    return 1 if result.refused else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
