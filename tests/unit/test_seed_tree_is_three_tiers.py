"""The seeded tree is three tiers, and every tier is actually wired.

Pure checks on the seed module's own tables, so they need no database: the
counts are the part that drifted twice without a test noticing.
"""

from __future__ import annotations


def test_every_seeded_department_has_system_instructions() -> None:
    """The agent's instructions are looked up by title, so a gap is a crash.

    Not hypothetical: renaming the departments to the six the owner named left
    three titles with no entry, and the seed died with `KeyError: 'Procurement
    Director'` on the way in. The two lists are the same seven names now, and this
    is what keeps them the same seven.

    It earned its keep again when IT was added as the seventh department: the
    department was seeded without a `SYSTEM_PROMPTS` entry, and this was the test
    that said so rather than a `KeyError` twenty seconds into a demo.
    """
    from ai_orchestrator.seed import DEPARTMENTS, SYSTEM_PROMPTS

    departments = DEPARTMENTS[1:]  # [0] is the executive, not a department
    assert len(departments) == 7, (
        f"the organisation is three tiers of one, three and seven: got "
        f"{[d.name for d in departments]}"
    )
    missing = [d.agent_title for d in departments if d.agent_title not in SYSTEM_PROMPTS]
    assert not missing, f"these departments would seed an agent with no instructions: {missing}"


def test_the_three_tiers_are_fully_specified() -> None:
    """Every department reports to an office that exists, and the shape is stated.

    Checked against the tree rather than the counts alone: a count of six with three
    reporting to nobody is the shape that passed twice before. **That orphan check is
    the reason this test exists and it is unchanged.**

    The per-office count used to be exactly two. Back Office now has three — Finance,
    HR and the new IT — because `ONX-BO-IT-SOP-007` (IT administration and access
    control) and `ONX-PMO-KNW-SOP-006` (the knowledge register) had no owner, and
    refusing them was not a resolution.

    So the count is **named rather than loosened to `>= 2`**. `assert len(names) >= 2`
    would have kept passing while the tree became six departments with one office
    empty and another carrying four, which is the failure this test was written to
    catch. Naming the distribution makes any future change a deliberate edit to this
    test rather than a silent drift past it.
    """
    from ai_orchestrator.seed import DEPARTMENTS, OFFICES

    office_slugs = {o.slug for o in OFFICES}
    per_office: dict[str, list[str]] = {slug: [] for slug in office_slugs}
    for dept in DEPARTMENTS[1:]:
        assert dept.parent_slug in office_slugs, (
            f"{dept.name} reports to {dept.parent_slug!r}, which is not an office"
        )
        per_office[dept.parent_slug].append(dept.name)

    assert set(per_office) == office_slugs
    expected = {"front-office": 2, "middle-office": 2, "back-office": 3}
    for slug, count in expected.items():
        assert len(per_office[slug]) == count, (
            f"{slug} has {len(per_office[slug])} departments "
            f"{per_office[slug]}, expected {count}. The tree is 7 departments in a "
            f"2 / 2 / 3 shape; changing that is a decision, not a drift."
        )
    assert sum(len(v) for v in per_office.values()) == 7
