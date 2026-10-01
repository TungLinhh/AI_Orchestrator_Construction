"""What a proposer profile is allowed to be.

One rule, and it is not a style preference: **a proposal must have been written by a
model.** Every other profile in the catalogue carries the deterministic provider as a
fallback, because for ordinary work a provider timeout degrading to a scripted answer
is a nuisance. For the proposer it is a different thing entirely. A canned proposal is
a lesson no model wrote, stamped with the name of a model that did not produce it,
rendered into a packet, hashed, and put in front of a human as a real suggestion. A
human cannot tell the difference by reading it.

So the rule is expressed as a check rather than as a comment, because a comment is
edited away the first time somebody adds a fallback "just to be safe".
"""

from __future__ import annotations

import pytest

from ai_orchestrator.models.profiles import default_profiles

pytestmark = pytest.mark.unit

#: The profile whose candidates must all be real models. Named here so the test fails
#: loudly if the profile is renamed, rather than quietly testing nothing.
PROPOSER = "proposer"


class TestTheProposerCannotBeAStub:
    def test_the_profile_exists(self) -> None:
        """Otherwise the loop is closed and nothing below is testing anything."""
        assert PROPOSER in default_profiles()

    def test_no_candidate_is_the_deterministic_provider(self) -> None:
        offenders = [
            c.model
            for c in default_profiles()[PROPOSER].candidates
            if c.provider == "deterministic"
        ]
        assert not offenders, (
            f"the proposer profile can answer with a script ({offenders}); a proposal "
            "nobody wrote would be put in front of a human as though a model had "
            "written it"
        )

    def test_it_has_no_fallback_profile(self) -> None:
        """A fallback to `default` reintroduces the stub through the back door."""
        assert default_profiles()[PROPOSER].fallback_profile is None, (
            "the proposer profile falls back to another profile, which may contain a "
            "scripted candidate"
        )

    def test_it_has_at_least_two_candidates(self) -> None:
        """One candidate is not a preference order, it is a single point of failure.

        Every free tier rate-limits, and a proposer that cannot be reached simply
        means no proposal — which is correct behaviour, but it also means the loop
        stops whenever one provider is having a bad afternoon.
        """
        assert len(default_profiles()[PROPOSER].candidates) >= 2

    def test_it_requires_tools(self) -> None:
        """The whole interface is one tool call. A proposer that cannot call tools
        returns prose, which the platform reads as 'no lesson' — indistinguishable
        from a model that looked and found nothing."""
        assert default_profiles()[PROPOSER].requires_tools is True


class TestItDiffersFromTheSubject:
    def test_the_proposer_is_not_the_operator_model(self) -> None:
        """The rule the profile exists to satisfy.

        `primary` is the model that does the work. If the proposer were the same
        model, every proposal would be the subject judging itself, and the platform
        refuses those — so the loop would close silently, producing nothing and
        looking like a system with no lessons to learn.
        """
        profiles = default_profiles()
        subject = {c.model for c in profiles["primary"].candidates}
        proposers = {c.model for c in profiles[PROPOSER].candidates}
        assert not (subject & proposers), (
            f"the proposer and the subject share a model: {subject & proposers}"
        )
