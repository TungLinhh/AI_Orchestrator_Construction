"""The seeded tree is three tiers, and every tier is actually wired.

Pure checks on the seed module's own tables, so they need no database: the
counts are the part that drifted twice without a test noticing.
"""

from __future__ import annotations


def test_every_seeded_department_has_system_instructions() -> None:
    """The agent's instructions are looked up by title, so a gap is a crash.

    Not hypothetical: renaming the departments to the six the owner named left
    three titles with no entry, and the seed died with `KeyError: 'Procurement
    Director'` on the way in. The two lists are the same six names now, and this
    is what keeps them the same six.
    """
    from ai_orchestrator.seed import DEPARTMENTS, SYSTEM_PROMPTS

    departments = DEPARTMENTS[1:]  # [0] is the executive, not a department
    assert len(departments) == 6, (
        f"the organisation is three tiers of one, three and six: got "
        f"{[d.name for d in departments]}"
    )
    missing = [d.agent_title for d in departments if d.agent_title not in SYSTEM_PROMPTS]
    assert not missing, f"these departments would seed an agent with no instructions: {missing}"


def test_the_three_tiers_are_fully_specified() -> None:
    """Two departments per office, every one of them under an office that exists.

    Checked against the tree rather than the counts alone: a count of six with
    three reporting to nobody is the shape that passed twice before.
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
    for slug, names in per_office.items():
        assert len(names) == 2, f"{slug} has {len(names)} departments: {names}"
