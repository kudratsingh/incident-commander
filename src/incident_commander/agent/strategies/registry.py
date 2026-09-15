"""The strategy registry: name → strategy, and a refusal for anything else.

Two guarantees, both structural:

* An unknown name is refused **at construction**, with the known names listed.
  A run that quietly fell back to ``baseline`` after a typo in
  ``INFERENCE_STRATEGY`` would report a number for the wrong strategy, and the
  report would look exactly like a correct one.
* The registry's keys are the ``StrategyName`` members, exactly — pinned by
  ``tests/unit/test_strategies.py``. ``INFERENCE_STRATEGY`` is typed as that
  enum, so a member with no entry here is a value that passes configuration and
  fails at run time, and an entry with no member is a strategy nothing can
  select (architecture principle 2: one source of truth for a mapping).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from incident_commander.agent.strategies.baseline import BaselineStrategy
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import InvestigationStrategy


class UnknownStrategyError(ValueError):
    """``INFERENCE_STRATEGY`` named something the registry does not have.

    Carries the known names in the message: the operator who typed the wrong
    one is the person who needs the list, and putting it in the exception means
    they get it from the failure rather than from the source.
    """

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        super().__init__(
            f"unknown inference strategy {name!r}; known strategies: {', '.join(known)}"
        )
        self.name = name
        self.known = known


class StrategyRegistry:
    """Name → factory, with the refusal above.

    Holds factories rather than instances so each run gets its own object: a
    strategy is free to carry per-run state later (a candidate cache, an
    iteration budget), and a shared singleton would leak it between scenarios
    in a 40-scenario suite.
    """

    def __init__(self, factories: Mapping[str, Callable[[], InvestigationStrategy]]) -> None:
        self._factories: dict[str, Callable[[], InvestigationStrategy]] = dict(factories)

    @property
    def names(self) -> tuple[str, ...]:
        """The registered names, sorted — a stable list for error messages."""
        return tuple(sorted(self._factories))

    def create(self, name: str) -> InvestigationStrategy:
        """Build the strategy ``name`` refers to, or raise ``UnknownStrategyError``.

        ``str(name)`` rather than ``name``: a ``StrategyName`` member is a
        ``str`` subclass and hashes the same, and the lookup should not care
        which of the two a caller has.
        """
        factory = self._factories.get(str(name))
        if factory is None:
            raise UnknownStrategyError(str(name), self.names)
        strategy = factory()
        if strategy.name != str(name):
            # A factory registered under the wrong key would stamp one name in
            # the provenance record while running another — the one way this
            # indirection could tell a lie about what produced a number.
            raise UnknownStrategyError(str(name), self.names)
        return strategy


#: The registry the application uses. One entry, and adding the next one is
#: this line plus a ``StrategyName`` member plus the strategy itself.
STRATEGIES: Final[StrategyRegistry] = StrategyRegistry(
    {StrategyName.BASELINE.value: BaselineStrategy}
)


def default_strategy() -> InvestigationStrategy:
    """The control group, for a caller with no configuration in hand.

    ``make_llm_investigate``'s default. It resolves ``StrategyName.BASELINE``
    through the registry rather than reading ``Settings``, because configuration
    is wired at the edge in this repo — nothing under ``agent/`` calls
    ``get_settings()``, and the investigation loop already takes its model, its
    budgets and its re-probe knobs as parameters. ``evals/runner.py`` is the
    edge: it resolves ``INFERENCE_STRATEGY`` through ``STRATEGIES.create`` and
    passes the result in. That the two agree — that the configured default and
    this default are both ``baseline`` — is pinned by
    ``tests/unit/test_strategies.py::TestTheDefaultIsBaseline``, so a config
    typo cannot silently change the control group in either direction.
    """
    return STRATEGIES.create(StrategyName.BASELINE.value)
