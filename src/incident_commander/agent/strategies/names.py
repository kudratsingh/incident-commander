"""The configurable strategy names — a closed set, on purpose.

Imports nothing, so ``config.Settings`` can type ``INFERENCE_STRATEGY`` as this enum without
dragging a strategy in.
"""

from __future__ import annotations

from enum import StrEnum


class StrategyName(StrEnum):
    """Every value ``INFERENCE_STRATEGY`` accepts.

    ``baseline`` is the control group. A member lands only *with* its implementation and its
    registry entry, never in advance.
    """

    BASELINE = "baseline"
    #: One planner call asked for several distinct diagnoses at once. This measures how well the
    #: model can enumerate alternatives, not the sampling metric the literature calls pass@k.
    BEST_OF_N_ENUMERATED = "best_of_n_enumerated"
    #: Several independent planner calls at a set temperature, whose candidate set is their top
    #: answers put together. Costs N planner calls, and is the sampling half of the comparison.
    BEST_OF_N_SAMPLED = "best_of_n_sampled"
    #: One of the arms above, plus an extra call that chooses between the candidates it produced.
    #: The arm is really three choices, so all three are stored with the run.
    CANDIDATE_SELECTOR = "candidate_selector"
    #: The baseline call, one critique of what it produced, and at most one revision after that.
    #: The single revision is enforced in code, not merely asked for in the critic's prompt.
    REFLECTION = "reflection"
    #: A bounded exploration of which read to make next: at most two levels deep and three
    #: branches wide, sharing one budget, and only on a replayed world, which cannot move.
    SEARCH = "search"
    #: The ladder: the baseline call on an easy step, and one more expensive rung for as long as
    #: an uncertainty signal is still firing after the rung below has run.
    ADAPTIVE = "adaptive"
