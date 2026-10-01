"""`ONX-BO-HR-SOP-004` as data, asserted against the SOP that is actually in the database.

The module `domain/hiring_process.py` encodes a process. These tests exist because a process
that only lives in code is a process nobody can check: the point of anchoring it to
`ONX-BO-HR-SOP-004` is that a re-issued SOP, or a typo in the code, fails **here** rather than
in a demo somebody ran once.

What is asserted, and what is deliberately not:

* the code exists in this tenant's catalogue, and the title matches **character for character**;
* every stage names an agent that the register actually contains;
* the stages form a chain: each one's office and department are ones the dossier's org chart
  uses, and the approval stages are the ones a person must decide;
* the three deliverables the SOP's *title* names all appear, so the process covers the SOP
  rather than a fragment of it.

Not asserted: that the eight stages are the SOP's stages. **They are not** — the register holds
the SOP's title and a hash, not its body (measured), so the stage list is the requested flow
arranged onto the dossier's forms. Pretending otherwise is the one thing these tests decline to
do, so the module docstring says it instead.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.hiring_process import (
    APPROVAL_OUTCOMES,
    DEMO_REQUEST,
    JD_SECTIONS,
    RUBRIC,
    SOP_CODE,
    SOP_TITLE,
    STAGES,
)

pytestmark = [pytest.mark.integration]


class TestItIsAnchoredToTheSopsThatExists:
    """Against the catalogue, in whichever tenant holds it.

    The first version of these two tests looked in the per-test tenant and **skipped** when it
    found nothing -- and the reference catalogue is not copied into each test tenant. It is
    copied once into a single holder organisation by `make test-fresh`, so a test scoped to a
    fresh tenant can never find it. A test that always skips is a test that always passes, so
    this asks the database where the catalogue actually is.
    """

    @staticmethod
    async def _catalogue_row() -> object | None:
        """The SOP's row, from whichever organisation holds the catalogue.

        `use_admin_role=True` because row-level security needs a tenant and this query has
        none. The catalogue has one row per organisation that holds it -- **measured at 19
        copies in the test database**, one per leaked test tenant -- so an RLS-scoped session
        with no tenant set reads **zero** of them. That is the control working correctly, so
        the fix is the connection, not the policy.

        The first version of this file looked in the per-test tenant and skipped when it found
        nothing, which made the test always pass and never run.
        """
        from ai_orchestrator.persistence.session import Database

        db = Database.from_settings(use_admin_role=True)
        try:
            async with db.session() as session:
                return (
                    (
                        await session.execute(
                            text(
                                "SELECT code, name_vi, department, owner_role_key "
                                "FROM sop_definitions WHERE code = :c "
                                "ORDER BY organization_id LIMIT 1"
                            ),
                            {"c": SOP_CODE},
                        )
                    )
                    .mappings()
                    .first()
                )
        finally:
            await db.dispose()

    async def test_the_code_is_in_the_catalogue_and_is_an_hr_sop(self) -> None:
        row = await self._catalogue_row()
        assert row is not None, (
            f"{SOP_CODE} is not in the reference catalogue. `make test-fresh` copies the dossier "
            "catalogue into a holder organisation; if this fails, that copy did not run"
        )
        assert row["department"] == "HR", "the SOP is a Human Resources one"

    async def test_the_title_matches_the_register_character_for_character(self) -> None:
        """**Exactly**, because the module's title is a quotation of it.

        Compared as plain string equality, not a normalised form. A previous incident in this
        repository turned an en dash into a hyphen inside a Vietnamese data string and the
        lookup that depended on it stopped matching; comparing normalised forms here would hide
        exactly that class of damage.
        """
        row = await self._catalogue_row()
        assert row is not None, f"{SOP_CODE} is not in the reference catalogue"
        assert row["name_vi"] == SOP_TITLE


class TestTheProcessItself:
    def test_every_stage_names_an_agent_that_exists(self) -> None:
        """The register plus the chief, and the "plus" is the point.

        `domain/agent_register.py` holds the dossier's **eight** agents. The Executive Agent is
        a ninth, created by `seed()` and not part of the register -- the first version of this
        test asserted against the register alone and failed on `Executive Agent`, which was the
        test being wrong about where the chief lives, not a stage naming an agent that is not
        there.

        The cost of getting this wrong in the other direction is a stage that names a typo and
        nobody notices, because nothing runs it until a person goes looking.
        """
        from ai_orchestrator.domain.agent_register import REGISTER

        known = {a.name for a in REGISTER} | {"Executive Agent"}
        missing = {s.agent_name for s in STAGES} - known
        assert not missing, (
            f"stages name agents that do not exist: {sorted(missing)}. A renamed agent must break "
            "here rather than produce a stage nobody can run"
        )

    def test_the_stages_form_a_numbered_chain(self) -> None:
        assert [s.n for s in STAGES] == list(range(1, len(STAGES) + 1)), (
            "the numbers are what a person reads in the UI, so a gap is a hole in the process"
        )

    def test_the_first_and_last_stage_are_the_ones_that_matter(self) -> None:
        """The chain has to start where the request enters and end where the person is useful.

        A process that starts at the paperwork misses the point that a vacancy is a *decision*
        first, and a process that ends at the offer has not onboarded anybody.
        """
        assert STAGES[0].approval, "stage 1 is a person saying yes before any work happens"
        assert STAGES[-1].key == "onboarding", (
            "the process must end with the person in the job, not with an offer letter"
        )

    def test_every_approval_names_a_decision_and_a_question(self) -> None:
        """An approval is a question, not a button.

        A gate whose only content is an action gives the person nothing to decide *about*, which
        is how HITL degrades into a rubber stamp without anybody noticing.
        """
        for stage in STAGES:
            if not stage.approval:
                continue
            assert stage.approval_question, (
                f"stage {stage.n} has an approval but no question for the person"
            )
            assert stage.approval_question.endswith("?"), (
                f"stage {stage.n}'s approval question is not phrased as a question: "
                f"{stage.approval_question!r}"
            )

    def test_the_approval_outcomes_are_the_dossier_four(self) -> None:
        """Tập 2 §E.2: a gate distinguishes conditional from held, so a boolean cannot carry it."""
        assert "APPROVED_WITH_CONDITIONS" in APPROVAL_OUTCOMES
        assert "HELD" in APPROVAL_OUTCOMES
        assert len(APPROVAL_OUTCOMES) == 4

    def test_the_rubric_is_the_dossier_three_levels(self) -> None:
        """Tập 3 §1.1: Tốt 100% / TB 60% / Kém 20%."""
        assert [name for name, _ in RUBRIC] == ["Tốt", "TB", "Kém"]
        assert [weight for _, weight in RUBRIC] == [1.00, 0.60, 0.20]

    def test_the_jd_has_the_dossier_six_sections(self) -> None:
        """Tập 3 Phần 3, and in the dossier's order."""
        assert len(JD_SECTIONS) == 6
        assert JD_SECTIONS[0] == "Mục đích vị trí"
        assert JD_SECTIONS[-1] == "Yêu cầu năng lực"

    def test_all_three_deliverables_of_the_sop_title_are_covered(self) -> None:
        """The SOP's title names four phases; a process that covers one is a fragment of it."""
        covered = {s.deliverable for s in STAGES}
        assert {"tuyển_dụng", "onboarding"} <= covered, (
            f"the process covers {sorted(covered)}, so it does not implement the SOP it cites"
        )

    def test_the_offboarding_phase_is_absent_and_that_is_named(self) -> None:
        """An honest gap, asserted so it cannot be forgotten.

        The SOP's title also names *Offboarding*, and no stage here covers it. The requested
        flow was recruitment through onboarding, and inventing an offboarding stage would be
        adding requirements nobody asked for. Asserted rather than left implicit, because a gap
        nobody wrote down is a gap nobody closes.
        """
        assert "offboarding" not in {s.deliverable for s in STAGES}


class TestTheDemonstrationRequest:
    def test_it_is_a_vacancy_the_dossier_has_a_job_description_for(self) -> None:
        """The demo vacancy is **Trưởng phòng Mua sắm**, one of Tập 3's six real JDs.

        Not invented: the dossier contains a JD for that position, so the JD the HR agent writes
        has a documented shape to match rather than being made up wholesale.
        """
        assert DEMO_REQUEST.position == "Trưởng phòng Mua sắm"
        assert "3.3" in DEMO_REQUEST.jd_reference

    def test_there_are_candidates_to_screen_and_score(self) -> None:
        assert len(DEMO_REQUEST.candidates) >= 3, (
            "scoring two candidates proves nothing about a rubric; three is the smallest that "
            "shows a ranking"
        )
        for c in DEMO_REQUEST.candidates:
            assert c.get("name") and c.get("note"), "a candidate with no note cannot be scored"

    def test_the_vacancy_is_in_one_of_the_six_departments(self) -> None:
        # The seed's own list, not a copy of it. The copy this used to read was a
        # third place the six were written down, and it had drifted -- which is how
        # three departments ended up rendered in the offices band (F217).
        from ai_orchestrator.seed import DEPARTMENTS

        keys = {d.slug for d in DEPARTMENTS[1:]}
        assert DEMO_REQUEST.department.lower() in keys, (
            "the demo vacancy must be in a department the product actually shows, or the "
            "demonstration runs somewhere nobody is looking"
        )
