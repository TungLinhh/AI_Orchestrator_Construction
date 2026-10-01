"""Every one of the 28 playbook SOPs is work a department in this build can be handed.

The brief was that department agents must be able to do the jobs in the playbook.
That is a coverage claim, and a coverage claim is only worth anything if a test
fails when it stops being true — so this file is mostly that test.

The two things it holds together:

* **The catalogue and the tree agree.** Every SOP the seed loads has an entry here,
  and every entry names a department the seed actually builds. A playbook that
  lists `BO-IT-SOP-007` and a tree with no IT department is not a gap that shows
  up anywhere else; this is where it shows up.
* **A stretched mapping is visible.** Several SOPs are run by a department that
  does not naturally own them, because the owner asked for six departments and the
  dossier has eighteen. That is a decision, and the test asserts each one is
  *marked* — so "we run it anyway" can never quietly become "we run it properly".

And the part that matters most: **the two SOPs with no home stay unassigned.**
Assigning them to a department that does not do the work would produce a plausible
answer to a question nobody asked — an organisation that believes its access
control and its knowledge base are handled. The honest output is a list.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.application.playbook import (
    AGENT_BY_DEPARTMENT,
    BY_CODE,
    FORBIDDEN_DECIDE,
    FORBIDDEN_STOP_WORK,
    PLAYBOOK,
    by_code,
    for_department,
    unassigned,
)

pytestmark = pytest.mark.unit

#: The dossier's own count, quoted from `A.2`: "Lịch sản xuất 28 SOP trong 12 tuần".
DOSSIER_COUNT = 28


@pytest.fixture
def spine_rows() -> dict[str, str]:
    """`{code: name}` as `scripts/seed_process_spine.py` loads it.

    The governance spine is the machine-readable half of the dossier, and it is
    what an operator's search would match against. Imported rather than copied, so
    a rename in the spine fails here rather than producing two truths.
    """
    import importlib.util
    from pathlib import Path

    spine = Path(__file__).resolve().parents[2] / "scripts" / "seed_process_spine.py"
    spec = importlib.util.spec_from_file_location("_spine_names", spine)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {code: name for code, name, *_rest in module.SOPS}


@pytest.fixture
def seeded_codes() -> set[str]:
    """The SOP codes the governance spine actually loads.

    A fixture rather than a constant so the catalogue is compared with the seed's
    own table. `test_agent_register_seeded` and friends are skipped by a
    `reset-test-db`, so nothing here may depend on a database being warm.
    """
    import importlib.util
    from pathlib import Path

    spine = Path(__file__).resolve().parents[2] / "scripts" / "seed_process_spine.py"
    spec = importlib.util.spec_from_file_location("_spine", spine)
    assert spec and spec.loader, "the governance spine is not where the test expects it"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {code for code, *_rest in module.SOPS}


class TestTheCatalogueIsWhole:
    def test_there_are_twenty_eight(self) -> None:
        assert len(PLAYBOOK) == DOSSIER_COUNT, (
            f"the dossier lists {DOSSIER_COUNT} SOPs; this catalogue has {len(PLAYBOOK)}"
        )

    def test_every_sop_the_seed_loads_has_an_entry(self, seeded_codes) -> None:
        """The catalogue and the database must not drift.

        Checked against the seeded rows rather than against a list in this file,
        so an SOP added to the governance spine without a procedure here fails.
        """
        missing = sorted(set(seeded_codes) - {sop.code for sop in PLAYBOOK})
        assert not missing, (
            f"the seed loads SOPs this catalogue does not describe: {missing}. "
            f"An SOP nobody can be handed is a row, not a process."
        )

    def test_every_entry_is_an_sop_the_seed_loads(self, seeded_codes) -> None:
        extra = sorted({sop.code for sop in PLAYBOOK} - set(seeded_codes))
        assert not extra, f"this catalogue describes SOPs the seed does not load: {extra}"

    def test_every_name_matches_the_spine_it_quotes(self, spine_rows) -> None:
        """The name is how a person finds a procedure.

        This caught a transcription error: `ONX-PMO-STD-SOP-005` was catalogued as
        "Quản lý **vòi** đời SOP" where the governance spine says "vòng". A
        catalogue that misquotes a procedure's title cannot be matched against the
        document it came from, and the mismatch is invisible until someone goes
        looking for it in the dossier.
        """
        mismatched = [
            (sop.code, sop.name, spine_rows.get(sop.code))
            for sop in PLAYBOOK
            if sop.code in spine_rows and spine_rows[sop.code] != sop.name
        ]
        assert not mismatched, (
            "these procedures are catalogued under a name the governance spine "
            f"does not use: {mismatched}"
        )

    def test_every_block_matches_the_code_it_claims(self, spine_rows) -> None:
        """`ONX-MO-…` is Middle Office, and a procedure filed under the wrong block
        would be routed to the wrong office's shelf."""
        from ai_orchestrator.domain.document_code import DocumentCode

        for sop in PLAYBOOK:
            parsed = DocumentCode.parse(sop.code)
            assert parsed is not None, f"{sop.code} is not a document code"
            assert parsed.block == sop.block, (
                f"{sop.code} says block {sop.block!r} but the code says {parsed.block!r}"
            )
            assert parsed.department == sop.dossier_department, (
                f"{sop.code} says department {sop.dossier_department!r} but the "
                f"code says {parsed.department!r}"
            )
        del spine_rows

    def test_no_code_is_defined_twice(self) -> None:
        codes = [sop.code for sop in PLAYBOOK]
        assert len(codes) == len(set(codes))

    def test_every_sop_has_steps_and_a_control_point(self) -> None:
        """A procedure with no steps is a title, and one with no control point has
        nothing to say *no* -- which is the sentence that matters most."""
        for sop in PLAYBOOK:
            assert sop.steps, f"{sop.code} has no steps"
            assert sop.control_point.strip(), f"{sop.code} has no control point"
            for step in sop.steps:
                assert step.strip(), f"{sop.code} has an empty step"


class TestEverySopIsCheckable:
    def test_a_runnable_sop_declares_what_it_must_produce(self) -> None:
        """The office can only review an answer against a declared contract.

        Without `required`, "completed" means the department wrote something, and
        a 3-way match that did not match looks exactly like a 3-way match that did.
        """
        for sop in PLAYBOOK:
            if not sop.runnable:
                continue
            assert sop.required, (
                f"{sop.code} is runnable but declares no output, so nothing can "
                f"check what the department produced"
            )

    def test_the_required_keys_are_distinct_and_lowercase_identifiers(self) -> None:
        for sop in PLAYBOOK:
            assert len(sop.required) == len(set(sop.required)), f"{sop.code} repeats a key"
            for key in sop.required:
                assert key.replace("_", "").isalnum(), f"{sop.code}: {key!r}"
                assert key == key.lower(), f"{sop.code}: {key!r}"

    def test_the_two_unassigned_sops_declare_nothing_on_purpose(self) -> None:
        """An SOP with no home must not carry a contract, because nothing can hold
        anyone to it. A contract on an unrunnable SOP is decoration."""
        for sop in unassigned():
            assert not sop.required, (
                f"{sop.code} has no department, so a declared output would be "
                f"a promise nobody is accountable for"
            )
            assert sop.note.strip(), (
                f"{sop.code} is unassigned and must say why, or it looks forgotten"
            )


class TestTheDepartmentsAreReal:
    def test_every_named_department_is_one_the_seed_builds(self) -> None:
        """Against the seed's own list, not a copy of it in the test."""
        from ai_orchestrator.seed import DEPARTMENTS

        built = {d.name for d in DEPARTMENTS[1:]}
        named = {sop.department for sop in PLAYBOOK if sop.department}
        assert not (named - built), (
            f"the playbook names departments the organisation does not have: "
            f"{sorted(named - built)}"
        )

    def test_every_named_department_has_an_agent(self) -> None:
        from ai_orchestrator.seed import DEPARTMENTS

        built = {d.agent_name for d in DEPARTMENTS[1:]}
        needed = {AGENT_BY_DEPARTMENT[sop.department] for sop in PLAYBOOK if sop.department}
        assert not (needed - built), (
            f"departments are mapped to agents that do not exist: {sorted(needed - built)}"
        )

    def test_no_department_is_left_without_work(self) -> None:
        """Six departments and none idle. A department with nothing to do is a
        department the owner is paying for to be a name on a page."""
        from ai_orchestrator.seed import DEPARTMENTS

        for dept in DEPARTMENTS[1:]:
            assert for_department(dept.name), f"{dept.name} has no SOP assigned to it"

    def test_the_office_pairing_agrees_with_the_tree(self) -> None:
        from ai_orchestrator.application.playbook import OFFICE_OF
        from ai_orchestrator.seed import DEPARTMENTS

        for dept in DEPARTMENTS[1:]:
            assert OFFICE_OF.get(dept.name) == dept.parent_slug, (
                f"{dept.name} reports to {dept.parent_slug} in the seed but "
                f"{OFFICE_OF.get(dept.name)} in the playbook's routing"
            )


class TestStretchedIsVisible:
    def test_every_stretched_mapping_says_why(self) -> None:
        """The dossier has eighteen departments and this build has six, so most of
        the catalogue is a stretch. Unmarked stretches are how a reader comes to
        believe a legal opinion was produced by a procurement agent."""
        for sop in PLAYBOOK:
            if sop.stretched:
                assert sop.note.strip(), (
                    f"{sop.code} is mapped to {sop.department} but does not say why"
                )

    def test_the_stretches_are_the_ones_we_expect(self) -> None:
        """Named, so that quietly moving an SOP between departments is a diff a
        reviewer sees rather than a silent improvement."""
        stretched = {sop.code for sop in PLAYBOOK if sop.stretched}
        # The legal opinion is the one judgement this build should not be trusted
        # to produce unsupervised, and it is on Procurement.
        assert "ONX-BO-LEG-SOP-006" in stretched
        assert by_code("ONX-BO-LEG-SOP-006").department == "Procurement"

    def test_no_sop_is_left_without_a_home(self) -> None:
        """The gap this test watched is closed, and this is where that is recorded.

        It used to assert exactly which two SOPs had no defensible owner:
        `ONX-BO-IT-SOP-007` (IT administration, access control, backup) and
        `ONX-PMO-KNW-SOP-006` (the knowledge register). Its own failure message said a
        seventh department would be the right answer, but it had to be deliberate.

        It was. **IT now exists under Back Office**, and both are its work — which is
        the only defensible reading, because an access-provisioning run inside Finance
        or QA produces a plausible answer to a question nobody asked.

        So the assertion is inverted from "these two are unmapped" to "none is", and
        strengthened: every SOP's department must be a department the seed actually
        builds. Asserting only that the list is empty would pass just as happily if a
        SOP were pointed at a department that does not exist.
        """
        from ai_orchestrator.seed import DEPARTMENTS

        left_over = sorted(sop.code for sop in unassigned())
        assert not left_over, (
            "a SOP has no owning department again. Refusing one is correct and is not "
            f"a resolution; give it a department that does the work. Now unassigned: "
            f"{left_over}"
        )

        seeded = {d.name for d in DEPARTMENTS[1:]}
        assert seeded == {
            "Sales",
            "Procurement",
            "QA/QC-HSE",
            "Design",
            "Finance",
            "HR",
            "IT",
        }, f"the roster changed; this test names it so a change is deliberate: {seeded}"

        stranded = {s.code: s.department for s in PLAYBOOK if s.department not in seeded}
        assert not stranded, f"these SOPs name a department the seed does not build: {stranded}"

        formerly_stranded = BY_CODE["ONX-BO-IT-SOP-007"], BY_CODE["ONX-PMO-KNW-SOP-006"]
        for sop in formerly_stranded:
            assert sop.department == "IT", (
                f"{sop.code} was the SOP with no defensible home; it is now owned by "
                f"{sop.department!r}, which is not the IT department"
            )


class TestTheForbiddenZones:
    def test_the_dossier_four_forbidden_decisions_are_carried(self) -> None:
        """`G.1` names them: hiring/firing decisions, DOA overrides, red-flagged
        suppliers, and stop-work orders. All four appear as a contract on a real
        SOP, which is what stops them being prose in a prompt."""
        forbidden = {sop.code: sop.forbidden for sop in PLAYBOOK if sop.forbidden}
        assert forbidden, "no SOP carries a forbidden zone at all"
        assert forbidden["ONX-BO-HR-SOP-004"] == FORBIDDEN_DECIDE
        assert forbidden["ONX-BO-HR-SOP-005"] == FORBIDDEN_DECIDE
        assert forbidden["ONX-PMO-GOV-SOP-001"] == FORBIDDEN_DECIDE
        assert forbidden["ONX-MO-HSE-SOP-008"] == FORBIDDEN_STOP_WORK

    def test_a_forbidden_sop_asks_for_preparation_not_a_decision(self) -> None:
        """The check that makes the zone mean something.

        `BO-FIN-SOP-002` may ask for `payment_decision`; a hiring SOP may not ask
        for `hired`. If the contract and the restriction ever disagree, the agent
        resolves it in whichever direction it is feeling, and the dossier's rule
        loses.
        """
        banned = ("decision_made", "hired", "rejected", "stop_work", "final_approval")
        for sop in PLAYBOOK:
            if not sop.forbidden:
                continue
            offenders = [k for k in sop.required if any(b in k for b in banned)]
            assert not offenders, (
                f"{sop.code} is in the AI-forbidden zone '{sop.forbidden}' but its "
                f"contract asks for {offenders}"
            )

    def test_the_task_brief_carries_the_zone_and_the_control_point(self) -> None:
        """They have to reach the agent, not live in a module docstring.

        The steps and the control point go in `input` so the office's review can
        read what the department was asked to do, and judge the answer against it.
        """
        for sop in PLAYBOOK:
            payload = sop.as_task_input()
            assert payload["control_point"] == sop.control_point
            assert payload["steps"] == list(sop.steps)
            if sop.forbidden:
                assert payload["forbidden_zone"] == sop.forbidden
                assert "Vùng cấm AI" in sop.control_point or "người quyết" in sop.control_point, (
                    f"{sop.code} claims a forbidden zone whose control point does "
                    f"not say a person decides"
                )


class TestTheCatalogueIsUsable:
    def test_by_code_finds_each_one_and_nothing_else(self) -> None:
        for sop in PLAYBOOK:
            assert by_code(sop.code) is sop
        assert by_code("ONX-NOPE-SOP-999") is None

    def test_the_three_tier_routing_is_complete(self) -> None:
        """Every runnable SOP can be routed: department, agent and office."""
        from ai_orchestrator.application.playbook import OFFICE_OF

        for sop in PLAYBOOK:
            if not sop.runnable:
                continue
            agent = AGENT_BY_DEPARTMENT[sop.department]
            office = OFFICE_OF[sop.department]
            assert agent and office, f"{sop.code} cannot be routed"
