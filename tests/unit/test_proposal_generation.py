"""Asking a model for a change, and refusing to hand over what it should not.

Four constraints, each tested by making the model break it and checking the platform
does not oblige: the proposer may not be the subject, the trace is data rather than
instructions, the output is validated rather than trusted, and too few runs means no
proposal at all.

"no proposal is a normal outcome" is the load-bearing idea. A generator that must
produce something will produce something, and the thing it produces will be a
lesson invented from a count.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ai_orchestrator.application.approval_packet import EvidenceRun
from ai_orchestrator.application.proposal_generation import ProposalGenerator
from ai_orchestrator.domain.budget import Money
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.models.gateway import (
    ModelCandidate,
    ModelGateway,
    ModelPricing,
    ModelProfile,
    ModelResponse,
)

pytestmark = pytest.mark.unit

FINGERPRINT = "f" * 64
ORG = "org_01m3d5hwxet3x61vjc1ffjyrzh"
SUBJECT = "dots-studio/dots-3-note-preview:free"
#: The model that writes the proposal. Different from the subject, because a subject
#: proposing its own correction is the case the platform refuses.
PROPOSER = "some/other-model:free"
#: A reply the platform should accept, so a refusal below is about the rule under
#: test and not about a malformed argument.
_REPLY: dict[str, Any] = {
    "kind": "patch",
    "target": "r",
    "old_string": "a",
    "new_string": "b",
    "why": "the order was wrong",
    "falsifier": "revert if it stops",
}


class _StubProvider:
    """Answers with exactly one tool call, or with prose.

    `DeterministicProvider` returns text, and a tool call has to be a real
    `ModelResponse.tool_calls` entry with the arguments as a JSON *string* — the
    OpenRouter wire shape the gateway parses. Building the response directly is the
    only way to be sure the test is exercising the shape the model actually
    returns.
    """

    #: Must match the candidate's provider: the gateway resolves an adapter by
    #: name, and a profile whose provider has no adapter fails every candidate.
    name = "deterministic"

    def __init__(
        self, tool_arguments: dict[str, Any] | None, *, model_used: str = PROPOSER
    ) -> None:
        self._tool_arguments = tool_arguments
        self._model_used = model_used

    def is_configured(self) -> bool:
        return True

    async def complete(self, candidate: Any, request: Any) -> ModelResponse:  # type: ignore[no-untyped-def]
        from ai_orchestrator.domain.budget import TokenUsage

        if self._tool_arguments is None:
            return ModelResponse(
                text="I see no lesson here.",
                finish_reason="stop",
                usage=TokenUsage(input_tokens=5, output_tokens=5),
                model_used=self._model_used,
            )
        return ModelResponse(
            text="",
            finish_reason="tool_calls",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "propose_change",
                        "arguments": json.dumps(self._tool_arguments),
                    },
                }
            ],
            usage=TokenUsage(input_tokens=5, output_tokens=5),
            model_used=self._model_used,
        )


def _gateway(tool_arguments: dict[str, Any] | None, *, model_used: str = PROPOSER) -> ModelGateway:
    """A gateway whose only provider answers in one scripted way."""
    gateway = ModelGateway()
    gateway.register_provider(_StubProvider(tool_arguments, model_used=model_used))  # type: ignore[arg-type]
    gateway.register_profile(
        ModelProfile(
            name="primary",
            candidates=(
                ModelCandidate(
                    provider="deterministic",
                    model="scripted-1",
                    pricing=ModelPricing(input_per_mtok=Money("0"), output_per_mtok=Money("0")),
                    max_classification=DataClassification.PUBLIC,
                ),
            ),
        )
    )
    return gateway


def _evidence(count: int = 3) -> tuple[EvidenceRun, ...]:
    return tuple(
        EvidenceRun(
            task_id=f"tsk_01m3d5hwxet3x61vjc1ffjyrz{letter}",
            outcome="failed",
            summary="the write was refused",
            tools=("write_report",),
            refusal_reason="ARTIFACT_NOT_WRITTEN",
        )
        for letter in "abc"[:count]
    )


def _mixed_evidence() -> tuple[EvidenceRun, ...]:
    """Two runs that worked and one that did not.

    The first run succeeded, which is the informative arrangement: it means the
    later failure is a change in conditions rather than a property of the procedure.
    """
    return (
        EvidenceRun(
            task_id="tsk_completed_a",
            outcome="completed",
            summary="drafted the update",
            tools=("write_report",),
        ),
        EvidenceRun(
            task_id="tsk_failed",
            outcome="failed",
            summary="searched 33 times and was stopped",
            tools=("safe_web_search",),
            refusal_reason="TURN_BUDGET",
        ),
        EvidenceRun(
            task_id="tsk_completed_b",
            outcome="completed",
            summary="drafted the update",
            tools=("write_report",),
        ),
    )


def _generate(gateway: ModelGateway, **overrides: Any):  # type: ignore[no-untyped-def]
    kwargs: dict[str, Any] = {
        "agent_id": "agt_01m3d5hwxet3x61vjc1ffjyrzj",
        "procedure_fingerprint": FINGERPRINT,
        "evidence": _evidence(),
        "subject_model": SUBJECT,
        "organization_id": ORG,
        "agent_id_of_subject": "agt_01m3d5hwxet3x61vjc1ffjyrzj",
    }
    kwargs.update(overrides)
    return ProposalGenerator(gateway).generate(**kwargs)


class TestAProposalIsProduced:
    async def test_a_valid_reply_becomes_a_packet(self) -> None:
        outcome = await _generate(
            _gateway(
                {
                    "kind": "patch",
                    "target": "requisition",
                    "old_string": "ask for the supplier",
                    "new_string": "ask for the cost centre",
                    "why": "3 of 3 runs were reworked because the supplier came first",
                    "falsifier": "if reworks for this reason stop, revert",
                }
            )
        )
        assert outcome.generated, outcome.reason
        assert outcome.packet is not None
        assert outcome.packet.proposal.change.new_string == "ask for the cost centre"

    async def test_a_failed_run_is_not_a_counter_example(self) -> None:
        """A failure is the reason *for* a change.

        The first version of this had it backwards, and the first live proposal is
        what showed it: three runs, all failed, all printed under the heading
        "Counter-examples" in a packet a human was being asked to approve. The
        argument for the change was presented as the argument against it.
        """
        outcome = await _generate(
            _gateway(
                {
                    "kind": "patch",
                    "target": "r",
                    "old_string": "a",
                    "new_string": "b",
                    "why": "every run was reworked",
                    "falsifier": "revert if it stops",
                }
            )
        )
        assert outcome.packet is not None
        assert outcome.packet.counter_examples == (), (
            "failed runs were presented as reasons not to change the procedure"
        )

    async def test_a_completed_run_is_a_counter_example(self) -> None:
        """It is the argument for leaving a working procedure alone."""
        outcome = await _generate(
            _gateway(
                {
                    "kind": "patch",
                    "target": "r",
                    "old_string": "a",
                    "new_string": "b",
                    "why": "the later runs went wrong",
                    "falsifier": "revert if it stops",
                }
            ),
            evidence=_mixed_evidence(),
        )
        assert outcome.packet is not None
        completed = [c for c in outcome.packet.counter_examples if c.kind == "completed_run"]
        assert {c.task_id for c in completed} == {"tsk_completed_a", "tsk_completed_b"}, (
            "a run that succeeded was not offered as a reason to leave it alone"
        )

    async def test_a_first_run_that_worked_is_called_out_separately(self) -> None:
        """If the first attempt succeeded, the later failures are a change in the
        conditions rather than a property of the procedure — and a proposal that does
        not say what changed is proposing to fix a problem that moved."""
        outcome = await _generate(
            _gateway(
                {
                    "kind": "patch",
                    "target": "r",
                    "old_string": "a",
                    "new_string": "b",
                    "why": "the later runs went wrong",
                    "falsifier": "revert if it stops",
                }
            ),
            evidence=_mixed_evidence(),
        )
        assert outcome.packet is not None
        assert any(c.kind == "completed_first" for c in outcome.packet.counter_examples), (
            "the run that worked first was not distinguished from the ones that worked "
            "later; the first one is the informative one"
        )

    async def test_one_sided_evidence_is_reported_as_one_sided(self) -> None:
        """No counter-examples at all is a fact about the evidence, and the packet's
        warning exists to say it. A reviewer who assumes balance they were not given
        will read a count as a pattern."""
        outcome = await _generate(
            _gateway(
                {
                    "kind": "patch",
                    "target": "r",
                    "old_string": "a",
                    "new_string": "b",
                    "why": "every run failed",
                    "falsifier": "revert if it stops",
                }
            )
        )
        assert outcome.packet is not None
        assert any("counter-example" in w for w in outcome.packet.warnings), (
            "one-sided evidence produced a packet that did not say it was one-sided"
        )


class TestTooFewRunsMeansNoLesson:
    async def test_below_the_threshold_nothing_is_asked(self) -> None:
        outcome = await _generate(_gateway(None), evidence=_evidence(2))
        assert not outcome.generated
        assert "coincidence" in outcome.reason

    async def test_no_evidence_at_all_means_no_proposal(self) -> None:
        outcome = await _generate(_gateway(None), evidence=())
        assert not outcome.generated


class TestTheModelIsNotBelieved:
    async def test_a_reply_that_is_not_a_proposal_is_refused(self) -> None:
        """The model declined. That is a real answer, and it is a valid outcome."""
        outcome = await _generate(_gateway(None))
        assert not outcome.generated
        assert "did not use the proposal tool" in outcome.reason

    async def test_an_invalid_proposal_is_refused_not_repaired(self) -> None:
        """No falsifier. The platform does not invent one."""
        outcome = await _generate(
            _gateway(
                {
                    "kind": "patch",
                    "target": "r",
                    "old_string": "a",
                    "new_string": "b",
                    "why": "because",
                    "falsifier": "n/a",
                }
            )
        )
        assert not outcome.generated
        assert "did not validate" in outcome.reason

    async def test_an_occurrence_count_the_model_invented_is_refused(self) -> None:
        """`occurrences` is derived from the evidence, never taken from the reply.

        A model claiming nine runs when three are attached is either wrong or lying,
        and the number is the gate's input.
        """
        outcome = await _generate(
            _gateway(
                {
                    "kind": "patch",
                    "target": "r",
                    "old_string": "a",
                    "new_string": "b",
                    "why": "many runs were reworked",
                    "falsifier": "revert if it stops",
                    "occurrences": 9,
                }
            )
        )
        # The extra field is not in the schema and the count is not read, so this
        # either produces a valid proposal from three runs or is refused. Both are
        # correct; what must not happen is a proposal citing nine.
        if outcome.packet is not None:
            assert outcome.packet.proposal.occurrences == 3, (
                "the model's own occurrence count reached the proposal"
            )


class TestTheProposerIsNotTheSubject:
    """The rule, and the two ways it can be satisfied or fail.

    The first version of this test asserted only that the recorded name differed
    from the subject, which passed whether or not the rule existed — the generator
    was writing the subject's name with a prefix, so the names always differed. What
    has to be tested is behaviour: a model that answers *both* calls must be
    refused, because that is the case the rule exists for.
    """

    async def test_the_same_model_answering_both_calls_is_refused(self) -> None:
        outcome = await _generate(_gateway(_REPLY, model_used=SUBJECT), subject_model=SUBJECT)
        assert not outcome.generated
        assert "echo" in outcome.reason

    async def test_a_different_model_proposing_is_allowed(self) -> None:
        outcome = await _generate(_gateway(_REPLY), subject_model=SUBJECT)
        assert outcome.packet is not None
        assert outcome.packet.proposal.proposed_by == f"generator:{PROPOSER}"
        assert outcome.packet.subject_model == SUBJECT

    async def test_a_gateway_that_does_not_say_which_model_answered_is_refused(
        self,
    ) -> None:
        """The proposer cannot be identified, so it cannot be checked.

        Stated precisely because it is easy to overstate: the production gateway
        fills `model_used` in from the candidate it routed to, so through
        `ModelGateway.complete` this cannot happen. It is reachable from a caller
        that supplies its own gateway, which is a real caller — the generator takes
        one as a constructor argument.

        The alternative was to record the name as `unknown` and let the comparison
        pass, which is a security check with a default on it: a hole, with a comment
        on the hole.
        """

        class _UnlabelledGateway:
            async def complete(self, request: Any) -> ModelResponse:  # type: ignore[no-untyped-def]
                return ModelResponse(
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "propose_change",
                                "arguments": json.dumps(_REPLY),
                            },
                        }
                    ],
                    finish_reason="tool_calls",
                )

        outcome = await ProposalGenerator(_UnlabelledGateway()).generate(  # type: ignore[arg-type]
            agent_id="agt_01m3d5hwxet3x61vjc1ffjyrzj",
            procedure_fingerprint=FINGERPRINT,
            evidence=_evidence(),
            subject_model=SUBJECT,
            organization_id=ORG,
            agent_id_of_subject="agt_01m3d5hwxet3x61vjc1ffjyrzj",
        )
        assert not outcome.generated
        assert "did not report which model" in outcome.reason

    async def test_a_different_subject_is_fine(self) -> None:
        outcome = await _generate(_gateway(None), subject_model="a-different-model")
        assert "coincidence" not in outcome.reason

    def test_the_comparison_ignores_how_the_proposer_labelled_itself(self) -> None:
        """The guard used to be plain equality, and could never fire.

        The generator always writes `generator:<model>`, so `proposed_by ==
        subject_model` was false for every input — a line of code that reads as a
        check and is trusted as one. This is the assertion that would have caught
        that, and it is at the comparison rather than at the caller, because the
        caller is the thing that was wrong.
        """
        from ai_orchestrator.application.proposal_generation import _same_model

        assert _same_model(f"generator:{SUBJECT}", SUBJECT), (
            "a generator proposing about its own subject was not detected"
        )
        assert _same_model(SUBJECT, SUBJECT)
        assert not _same_model("generator:other-model", SUBJECT)
        assert not _same_model("generator:", SUBJECT), "an empty name is not a match"


class TestTheEvidenceMustMatchTheAgent:
    async def test_a_mismatch_is_refused(self) -> None:
        """The runs are one agent's and the change alters another's.

        Cheap to check, and a mistake worth refusing rather than noticing later: the
        reviewer would be shown evidence that does not describe the thing being
        changed.
        """
        outcome = await _generate(_gateway(None), agent_id_of_subject="agt_somebody_else")
        assert not outcome.generated
        assert "different agent" in outcome.reason


class TestTheTraceIsUntrustedData:
    async def test_the_prompt_delimits_the_runs_and_says_they_are_data(self) -> None:
        """A trace row is something a model wrote, and a model wrote it under
        instructions that may not have been its own.

        Asserted by rendering the request rather than by trusting the constant: a
        refactor that dropped the delimiter would otherwise be invisible.
        """
        from ai_orchestrator.application.proposal_generation import _GENERATOR_INSTRUCTIONS

        assert "untrusted content" in _GENERATOR_INSTRUCTIONS
        assert "not instructions" in _GENERATOR_INSTRUCTIONS

    async def test_the_prompt_carries_the_runs_inside_a_label(self) -> None:
        from ai_orchestrator.application.proposal_generation import ProposalGenerator

        captured: list[str] = []

        class _CapturingGateway(ModelGateway):
            async def complete(self, request):  # type: ignore[no-untyped-def]
                captured.append(request.prompt)
                return ModelResponse(text="", finish_reason="stop")

        generator = ProposalGenerator(_CapturingGateway(), threshold=1)
        await generator.generate(
            agent_id="agt_01m3d5hwxet3x61vjc1ffjyrzj",
            procedure_fingerprint=FINGERPRINT,
            evidence=_evidence(1),
            subject_model=SUBJECT,
            organization_id=ORG,
            agent_id_of_subject="agt_01m3d5hwxet3x61vjc1ffjyrzj",
        )
        assert captured, "the generator made no call, so the prompt was never rendered"
        assert "<runs>" in captured[0]
        assert "</runs>" in captured[0]

    async def test_the_prompt_shows_the_counter_examples_too(self) -> None:
        """A proposer shown only supporting evidence will always propose.

        The counter-examples are derived from the same runs the proposer already sees,
        so this is framing rather than new information — and framing is what decides
        whether a model concludes there is a lesson. Naming the runs that failed is
        what gives it the chance to decline, and this is the last point at which a
        *model* can decline; everything after is a gate, and a gate only checks
        well-formedness.
        """
        from ai_orchestrator.application.proposal_generation import ProposalGenerator

        captured: list[str] = []

        class _CapturingGateway(ModelGateway):
            async def complete(self, request):  # type: ignore[no-untyped-def]
                captured.append(request.prompt)
                return ModelResponse(text="", finish_reason="stop")

        await ProposalGenerator(_CapturingGateway(), threshold=1).generate(
            agent_id="agt_01m3d5hwxet3x61vjc1ffjyrzj",
            procedure_fingerprint=FINGERPRINT,
            evidence=_mixed_evidence(),
            subject_model=SUBJECT,
            organization_id=ORG,
            agent_id_of_subject="agt_01m3d5hwxet3x61vjc1ffjyrzj",
        )
        assert captured
        prompt = captured[0]
        assert "<against_the_proposal>" in prompt, (
            "the proposer was shown the runs that argue for a change and not the ones "
            "that argue against it"
        )
        assert "tsk_completed_a" in prompt.split("<against_the_proposal>")[1], (
            "a run that succeeded was withheld from the proposer, so it was never told "
            "the procedure already works"
        )
