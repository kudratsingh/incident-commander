"""A judge client scripted by the question it is asked.

``llm.fakes.CannedLLMClient`` plays a fixed SEQUENCE, which is the right shape
for a scenario walking a run from triage to resolution and the wrong shape here:
a calibration asks the same judge many unrelated questions, five times each, and
scripting that as one flat list means the script silently shifts the moment a
trap is added or reordered.

So this fake is keyed on the **user message** — the judge's own rendered context,
which the harness can compute before the call because every subject can render
itself (``roles.JudgeSubject.context``). Two consequences worth having:

* **No tell in the context.** Keying on the case id would mean putting the id
  into what the judge reads, and a judge that can see which trap it is on is not
  being trapped. The key is the question itself.
* **Instability is scriptable.** Each key maps to a SEQUENCE of payloads,
  consumed in order with the last one repeating — the same rule
  ``evals/fakes.CannedMCPClient`` uses. ``["a", "a", "b"]`` is a judge that
  changes its mind on the third ask, which is exactly what the self-agreement
  leg has to be able to measure and fail on.

An unscripted question raises rather than defaulting. A fake that invented a
verdict for a question nobody scripted would make a green calibration test prove
nothing — the failure mode ``CannedLLMClient`` avoids the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from evals.judge_calibration.roles import (
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CANDIDATE_SELECTOR,
    SELECT_PREFIX,
    SelectionSubject,
)
from evals.judge_calibration.traps import TrapCase, traps_for
from incident_commander.llm.client import LLMError, LLMResult


class FakeJudgeClient:
    """Structural ``LLMClientProtocol`` fake, scripted per rendered question.

    ``answers`` maps a user message to the payloads to return for it, in order.
    ``calls`` records every ``(system_prompt, user_message)`` and
    ``temperatures`` every temperature sent, so a test can assert that the
    calibration asked through the real prompt and sent no sampling parameter
    (ADR 0048 / decision O-24) rather than assume it.
    """

    def __init__(self, answers: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        self._answers: dict[str, list[Mapping[str, Any]]] = {}
        for question, payloads in answers.items():
            queue = list(payloads)
            if not queue:
                raise ValueError(
                    "empty answer sequence for a judge question; a scripted "
                    "question with no payload cannot be asked even once"
                )
            self._answers[question] = queue
        self._served: dict[str, int] = dict.fromkeys(self._answers, 0)
        self.calls: list[tuple[str, str]] = []
        self.temperatures: list[float | None] = []

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> LLMResult[T]:
        self.calls.append((system_prompt, user_message))
        self.temperatures.append(temperature)
        question = self._question(user_message)
        queue = self._answers.get(question) if question is not None else None
        if question is None or queue is None:
            raise LLMError(
                "no scripted judge answer for this question. The fake is keyed on "
                "the rendered context, so a question it has not been given is "
                "either a new trap case or a context renderer that changed shape; "
                "both are things a test should notice rather than a default it "
                f"should absorb. First line asked: {user_message.splitlines()[:1]}"
            )
        index = min(self._served[question], len(queue) - 1)
        self._served[question] += 1
        return LLMResult(
            output=output_model.model_validate(dict(queue[index])),
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            stop_reason="canned",
        )

    def _question(self, user_message: str) -> str | None:
        """Which scripted question this message is, exact match or repair re-ask.

        ADR 0035's re-ask sends the ORIGINAL turn plus the repair turn appended
        (``repair.repair_message``), so a repaired call arrives with a longer
        message than the one that was scripted. Matching on the prefix is what
        lets a test script "malform once, then answer" without the fake having to
        know the repair prompt's bytes — and the repair path is exactly what has
        to be exercisable here, because it is the thing WO-R2-174 added to both
        judges and the thing that makes calibrating them possible.

        Exact match first, then the longest scripted prefix, so a question that is
        itself a prefix of another cannot shadow it.
        """
        if user_message in self._answers:
            return user_message
        matches = [key for key in self._answers if user_message.startswith(key)]
        return max(matches, key=len) if matches else None

    def asked(self, question: str) -> int:
        """How many times one question was put. Zero for one never asked."""
        return self._served.get(question, 0)


def payload_for(case: TrapCase, verdict: str) -> dict[str, Any]:
    """The structured-output payload a judge emitting ``verdict`` would send.

    The inverse of ``roles``' verdict projection, and it has to go through the
    real schema: the payload is handed to ``output_model.model_validate``, so a
    selector payload that scored the wrong set of candidate ids, or a score
    outside 0-1, is rejected by the same validators a real reply meets. A fake
    that could emit something the schema forbids would let a test pass on a
    payload no model could have sent.

    ``reasoning`` is filled with the fact that this came from a fake rather than
    with plausible prose. A scripted report that reads like a real one is how a
    number produced by a script ends up quoted as a measurement.
    """
    reasoning = f"scripted fake judge answer for trap {case.case_id}; not a measurement"
    if case.judge == ACTION_VERIFIER:
        return {"verdict": verdict, "reasoning": reasoning}
    if case.judge == BRIEFING_JUDGE:
        grounded = "grounded=yes" in verdict
        actionable = "actionable=yes" in verdict
        return {
            "groundedness": _score(grounded),
            "actionability": _score(actionable),
            "reasoning": reasoning,
        }
    if case.judge == CANDIDATE_SELECTOR:
        subject = case.subject
        assert isinstance(subject, SelectionSubject)
        ids = [candidate.candidate_id for candidate in subject.candidates]
        chosen = verdict[len(SELECT_PREFIX) :] if verdict.startswith(SELECT_PREFIX) else None
        decision = "select" if chosen is not None else verdict
        # The chosen candidate has to be the top score: on `probe_more` the loop
        # reads the highest-scored candidate's next probe (ADR 0048), so a payload
        # whose scores disagreed with its own decision would be a fake that does
        # not behave like the thing it stands in for.
        top = chosen if chosen is not None else ids[0]
        return {
            "decision": decision,
            "selected_candidate_id": chosen,
            "scores": {
                candidate_id: (0.85 if candidate_id == top else 0.2) for candidate_id in ids
            },
            "uncertainty": 0.2 if chosen is not None else 0.7,
            "reasoning": reasoning,
        }
    raise KeyError(f"no fake payload shape for judge {case.judge!r}")


def _score(high: bool) -> float:
    """A dimension either clearly above the useful bar or clearly below it.

    Not 0.7 exactly: the projection thresholds at ``USEFUL_THRESHOLD``, and a
    fake that sat on the boundary would make every test of the projection a test
    of a float comparison.
    """
    return 0.9 if high else 0.2


def answers_for(
    judge: str,
    *,
    wrong: Mapping[str, str] | None = None,
    unstable: Mapping[str, str] | None = None,
    reps: int = 5,
) -> dict[str, list[Mapping[str, Any]]]:
    """A script for one judge's whole trap set, keyed by rendered question.

    By default every case is answered with the verdict it asserts — a judge that
    agrees. ``wrong`` replaces one case's answer outright (``{case_id: verdict}``)
    and ``unstable`` makes a case answer its asserted verdict for the first
    ``reps - 1`` asks and the given verdict on the last, which is the shape the
    self-agreement leg has to be able to catch.

    Built from the trap set rather than written out, so adding a trap cannot leave
    the script silently one case short: an unscripted question raises.
    """
    wrong = wrong or {}
    unstable = unstable or {}
    script: dict[str, list[Mapping[str, Any]]] = {}
    for case in traps_for(judge):
        answer = wrong.get(case.case_id, case.asserts)
        payloads: list[Mapping[str, Any]] = [payload_for(case, answer)]
        if case.case_id in unstable:
            payloads = [payload_for(case, answer)] * max(reps - 1, 1) + [
                payload_for(case, unstable[case.case_id])
            ]
        script[case.context()] = payloads
    return script
