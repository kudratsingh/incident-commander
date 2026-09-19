"""A judge client scripted by the question it is asked.

``llm.fakes.CannedLLMClient`` plays a fixed SEQUENCE, which is wrong here: a
calibration asks many unrelated questions five times each, and a flat list shifts the
moment a trap is added. So this fake is keyed on the USER MESSAGE — the rendered
context, which the harness can compute in advance. Keying on the case id would put a
tell in what the judge reads, and a judge that can see which trap it is on is not being
trapped. Each key maps to a SEQUENCE consumed in order with the last repeating, so
instability is scriptable, which the self-agreement leg has to be able to fail on. An
unscripted question RAISES rather than inventing a verdict.
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

    ``answers`` maps a user message to its payloads, in order. ``calls`` and
    ``temperatures`` record what was asked, so a test can ASSERT that the calibration
    went through the real prompt and sent no sampling parameter (ADR 0048, O-24).
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

        ADR 0035's re-ask appends the repair turn, so a repaired call arrives longer
        than what was scripted; matching on the PREFIX lets a test script "malform once,
        then answer" without knowing the repair prompt's bytes. Exact match first, then
        the longest prefix, so a question that is a prefix of another cannot shadow it.
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

    The inverse of ``roles``' projection, through the REAL schema: the payload meets
    ``output_model.model_validate``, so a fake cannot emit something no model could have
    sent. ``reasoning`` says it came from a fake rather than reading like real prose,
    because that is how a scripted number ends up quoted as a measurement.
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
        # The chosen candidate has to be the top score: on `probe_more` the loop reads
        # the highest-scored candidate's next probe (ADR 0048).
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

    Not ``USEFUL_THRESHOLD`` exactly: a fake sitting on the boundary would make every
    test of the projection a test of a float comparison.
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

    Every case answers the verdict it asserts by default. ``wrong`` replaces one
    outright; ``unstable`` answers correctly for ``reps - 1`` asks and differently on the
    last, which is the shape the self-agreement leg must catch. Built FROM the trap set,
    so adding a trap cannot leave the script one case short — an unscripted question raises.
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
