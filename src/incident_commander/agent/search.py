"""The search node, its score and the bounds a walk is held to (plan 02 § 14, WP-12.1).

The bounds are objects that raise, not prompt text (ADR 0055), and a walk spends the run's OWN
ledger: tool calls are never multiplied, so every branch competes with the chosen path.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from incident_commander.agent.hypothesis import Hypothesis, ProbeAction
from incident_commander.agent.state import BudgetLedger, EvidenceEntry

#: How many levels below the root a walk may go, and how many branches each node may open.
#: Configuration may ask for less than these, never more.
MAX_SEARCH_DEPTH: Final[int] = 2
MAX_BRANCH_FACTOR: Final[int] = 3

#: A branch makes one read, and one tool call is always held back so the chosen path can still
#: make its own: exploring must never use up the budget the acting path needs.
BRANCH_PROBE_TOOL_CALLS: Final[int] = 1
CHOSEN_PATH_TOOL_RESERVE: Final[int] = 1

#: The weights in a node's score: the selector's confidence, less tool cost, token cost and
#: safety risk. Declared rather than tuned — tuning them needs a paid sweep first.
TOOL_COST_WEIGHT: Final[float] = 0.25
TOKEN_COST_WEIGHT: Final[float] = 0.25
SAFETY_RISK_WEIGHT: Final[float] = 0.5

#: The exact wording each refusal uses, so a test can assert the reason rather than merely that
#: something raised.
DEPTH_CAP_SPENT: Final[str] = "this step's search depth is already spent"
BRANCH_CAP_SPENT: Final[str] = "this node's branches are already spent"
DEPTH_ABOVE_MAXIMUM: Final[str] = "requested search depth is above the structural maximum"
BRANCH_ABOVE_MAXIMUM: Final[str] = "requested branch factor is above the structural maximum"

#: Why the shared budget refused another branch. Each is a reason a reader can act on: a walk
#: that explored less than it was allowed and one that hit its limit are different findings.
LEDGER_EXHAUSTED: Final[str] = "ledger exhausted"
#: Two candidates proposing the same read are one decision, so the second opens no branch: it
#: would pay for a second call to get an answer already in hand.
DUPLICATE_BRANCH_PROBE: Final[str] = "duplicate probe"
TOOL_CEILING_RESERVED: Final[str] = "tool-call ceiling reserved for the chosen path"
TOKEN_CEILING_RESERVED: Final[str] = "token ceiling reserved for the chosen path"

#: The refusal a run gets unless it is replaying a recording, written once for both the strategy
#: and the eval runner: branches of a live world read a world that moves underneath them.
SEARCH_IS_RECORDED_MODE_ONLY: Final[str] = (
    "search runs in RECORDED mode only. A branch gathers evidence, so in a live world "
    "each branch would read a world that had already moved and no two branches would be "
    "comparable; a canned world answers per-tool sequences rather than per-call, so a "
    "second branch would be served the first branch's answer. Record the world "
    "(`make world-record ONLY=<scenario>`) and replay it with `--mode recorded`, or run a "
    "strategy that makes one planner call per step."
)


class SearchCapExceeded(RuntimeError):
    """A walk asked for a level or a branch it had already spent.

    A ``RuntimeError`` rather than an ``LLMError`` (which the loop escalates on): a breached
    bound is a defect here, not an incident outcome.
    """

    def __init__(self, marker: str, spent: int, allowed: int) -> None:
        super().__init__(
            f"{marker} ({spent} of {allowed} used). Search is bounded to depth "
            f"{MAX_SEARCH_DEPTH} and branch {MAX_BRANCH_FACTOR} (plan 02 § 14); past "
            "that it is unbounded exploration on a shared budget."
        )
        self.marker = marker
        self.spent = spent
        self.allowed = allowed


@dataclass(slots=True)
class BranchAllowance:
    """One node's branches: spendable ``allowed`` times, then it raises."""

    allowed: int
    spent: int = 0

    def spend(self) -> None:
        """Take one branch, or raise ``SearchCapExceeded``."""
        if self.spent >= self.allowed:
            raise SearchCapExceeded(BRANCH_CAP_SPENT, self.spent, self.allowed)
        self.spent += 1

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.allowed


@dataclass(slots=True)
class SearchWalk:
    """One planner step's whole search budget: its levels, and each node's branches.

    Bounds as objects rather than loop shape, so an edit that wrapped the walk in a second
    loop raises here instead of exploring twice as far.
    """

    depth_allowed: int = MAX_SEARCH_DEPTH
    branch_allowed: int = MAX_BRANCH_FACTOR
    depth_spent: int = 0

    def __post_init__(self) -> None:
        """Refuse a request above the structural maximum rather than clamping it: clamping
        would run a 3-branch walk for a caller who asked for 4 and report the 4."""
        if self.depth_allowed > MAX_SEARCH_DEPTH or self.depth_allowed < 1:
            raise SearchCapExceeded(DEPTH_ABOVE_MAXIMUM, self.depth_allowed, MAX_SEARCH_DEPTH)
        if self.branch_allowed > MAX_BRANCH_FACTOR or self.branch_allowed < 1:
            raise SearchCapExceeded(BRANCH_ABOVE_MAXIMUM, self.branch_allowed, MAX_BRANCH_FACTOR)

    def descend(self) -> None:
        """Take one level, or raise ``SearchCapExceeded``."""
        if self.depth_spent >= self.depth_allowed:
            raise SearchCapExceeded(DEPTH_CAP_SPENT, self.depth_spent, self.depth_allowed)
        self.depth_spent += 1

    @property
    def exhausted(self) -> bool:
        return self.depth_spent >= self.depth_allowed

    def branches(self) -> BranchAllowance:
        """A fresh branch allowance for the node being expanded."""
        return BranchAllowance(allowed=self.branch_allowed)


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeCost:
    """What one node of the walk cost: the tool calls, tokens and dollars it added."""

    tool_calls: int = 0
    tokens: int = 0
    usd: Decimal = Decimal("0")

    def plus(self, other: NodeCost) -> NodeCost:
        return NodeCost(
            tool_calls=self.tool_calls + other.tool_calls,
            tokens=self.tokens + other.tokens,
            usd=self.usd + other.usd,
        )


def cost_between(before: BudgetLedger, after: BudgetLedger) -> NodeCost:
    """What the ledger moved by — measured, never counted a second time by the strategy."""
    return NodeCost(
        tool_calls=after.tool_calls_used - before.tool_calls_used,
        tokens=after.tokens_used - before.tokens_used,
        usd=after.usd_used - before.usd_used,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeScore:
    """A node's score, kept as its four separate terms so a reader sees why a path won."""

    selector_confidence: float
    tool_cost: float
    token_cost: float
    safety_risk: float

    @property
    def total(self) -> float:
        """``selector_confidence − tool_cost − token_cost − safety_risk``, verbatim."""
        return self.selector_confidence - self.tool_cost - self.token_cost - self.safety_risk


def score_node(
    *,
    selector_confidence: float,
    uncertainty: float,
    commits: bool,
    cost: NodeCost,
    ceiling: BudgetLedger,
) -> NodeScore:
    """Score one node from the selector's own numbers and the ledger's own deltas.

    ``safety_risk`` is the selector's uncertainty charged only against a node that would
    COMMIT: gathering more evidence carries none, and acting while unsure is the risk.
    """
    return NodeScore(
        selector_confidence=selector_confidence,
        tool_cost=TOOL_COST_WEIGHT * _fraction(cost.tool_calls, ceiling.max_tool_calls),
        token_cost=TOKEN_COST_WEIGHT * _fraction(cost.tokens, ceiling.max_tokens),
        safety_risk=(SAFETY_RISK_WEIGHT * uncertainty) if commits else 0.0,
    )


def _fraction(used: int, ceiling: int) -> float:
    """``used / ceiling``, bounded to 1.0; a zero ceiling is fully spent, not free."""
    if ceiling <= 0:
        return 1.0
    return min(used / ceiling, 1.0)


def room_for_a_branch(budget: BudgetLedger, *, token_reserve: int) -> str | None:
    """Why the shared ledger cannot afford another branch, or ``None`` when it can.

    ``token_reserve`` is the largest single call this step already paid for — measured, not
    guessed — held back so the chosen path can still make its own call.
    """
    if budget.is_exhausted:
        return LEDGER_EXHAUSTED
    if (
        budget.tool_calls_used + BRANCH_PROBE_TOOL_CALLS + CHOSEN_PATH_TOOL_RESERVE
        > budget.max_tool_calls
    ):
        return TOOL_CEILING_RESERVED
    if budget.tokens_used + token_reserve > budget.max_tokens:
        return TOKEN_CEILING_RESERVED
    return None


def new_node_id() -> str:
    """A 12-hex node id — the same width and mint as the trace store's record ids."""
    return uuid.uuid4().hex[:12]


def evidence_snapshot_ref(evidence: Sequence[EvidenceEntry]) -> str:
    """A 12-hex ref for one node's evidence snapshot: the ledger's ids, in order.

    A ref, not a copy — the entries are in the trace already, and two branches are told
    apart by which readings they hold.
    """
    digest = hashlib.sha256()
    for entry in evidence:
        digest.update(str(entry.evidence_id).encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:12]


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchNode:
    """One node of the walk: the ranking at it, a reference to the evidence it was scored over,
    the read it would make next, its score and cost, its place in the tree, and any refusal."""

    node_id: str
    #: Empty on the root node, which is the step the generator proposed before any read.
    parent_id: str = ""
    depth: int
    evidence_snapshot_ref: str
    hypotheses: tuple[Hypothesis, ...] = ()
    #: The read this node would make next; ``None`` where its path would stop or remediate.
    proposed_probe: ProbeAction | None = None
    #: The read that was actually made to reach this node. A branch IS a choice of which read
    #: to make, so this is what tells one branch from another.
    probe_taken: ProbeAction | None = None
    score: NodeScore
    cost: NodeCost
    #: Why this branch never became a scored node — the read was not allowed, the recording had
    #: no answer for it, or the shared budget was spent. ``None`` on every node that ran.
    refused: str | None = None
