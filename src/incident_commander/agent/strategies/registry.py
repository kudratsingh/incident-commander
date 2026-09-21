"""The strategy registry: name → strategy, and a refusal for anything else.

An unknown ``INFERENCE_STRATEGY`` is refused **at construction**: a quiet fallback to
``baseline`` would report a number for the wrong strategy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from incident_commander.agent.strategies.adaptive import AdaptiveStrategy
from incident_commander.agent.strategies.baseline import BaselineStrategy
from incident_commander.agent.strategies.best_of_n_enumerated import BestOfNEnumeratedStrategy
from incident_commander.agent.strategies.best_of_n_sampled import BestOfNSampledStrategy
from incident_commander.agent.strategies.candidate_selector import CandidateSelectorStrategy
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import InvestigationStrategy
from incident_commander.agent.strategies.reflection import ReflectionStrategy
from incident_commander.agent.strategies.search import SearchStrategy

#: Takes a settings block and returns one strategy. Every factory takes the block, even those
#: with nothing to read from it, so ``create`` needs no special case for any of them.
StrategyFactory = Callable[[StrategyKnobs], InvestigationStrategy]


class UnknownStrategyError(ValueError):
    """``INFERENCE_STRATEGY`` named something the registry does not have. Carries the known
    names, because the operator who typed it needs the list."""

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        super().__init__(
            f"unknown inference strategy {name!r}; known strategies: {', '.join(known)}"
        )
        self.name = name
        self.known = known


class StrategyRegistry:
    """Name → factory, with the refusal above. Factories, not instances: a shared singleton
    would leak per-run state between scenarios."""

    def __init__(self, factories: Mapping[str, StrategyFactory]) -> None:
        self._factories: dict[str, StrategyFactory] = dict(factories)

    @property
    def names(self) -> tuple[str, ...]:
        """The registered names, sorted — a stable list for error messages."""
        return tuple(sorted(self._factories))

    def create(self, name: str, knobs: StrategyKnobs | None = None) -> InvestigationStrategy:
        """Build the strategy ``name`` refers to, or raise ``UnknownStrategyError``.

        ``knobs=None`` means the control group's defaults, so a caller that forgot to configure
        gets ``baseline``'s shape rather than an arm it did not choose.
        """
        factory = self._factories.get(str(name))
        if factory is None:
            raise UnknownStrategyError(str(name), self.names)
        strategy = factory(knobs if knobs is not None else StrategyKnobs())
        if strategy.name != str(name):
            # A factory registered under the wrong key would store one strategy's name with
            # the run while actually running another, so refuse instead.
            raise UnknownStrategyError(str(name), self.names)
        return strategy


#: The registry the application uses. Adding a strategy is one line here, one ``StrategyName``
#: member, and the strategy itself.
STRATEGIES: Final[StrategyRegistry] = StrategyRegistry(
    {
        StrategyName.BASELINE.value: BaselineStrategy,
        StrategyName.BEST_OF_N_ENUMERATED.value: BestOfNEnumeratedStrategy,
        StrategyName.BEST_OF_N_SAMPLED.value: BestOfNSampledStrategy,
        StrategyName.CANDIDATE_SELECTOR.value: CandidateSelectorStrategy,
        StrategyName.REFLECTION.value: ReflectionStrategy,
        StrategyName.SEARCH.value: SearchStrategy,
        StrategyName.ADAPTIVE.value: AdaptiveStrategy,
    }
)


def default_strategy() -> InvestigationStrategy:
    """The control group, for a caller with no configuration in hand.

    ``make_llm_investigate``'s default, resolved through the registry rather than by reading
    ``Settings``; ``evals/runner.py`` is the edge that resolves ``INFERENCE_STRATEGY``.
    """
    return STRATEGIES.create(StrategyName.BASELINE.value)
