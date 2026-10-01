"""Seed the dossier's eight agents, their procedures, and their grants.

`python scripts/seed_agent_register.py --org <organization_id>`

## What it writes, in order, and why that order

1. **`procedures` + `procedure_versions`** for the sixteen SOPs, in `shadow`, through
   `propose_version`. A version in `shadow` changes nothing, which is the property the whole
   governance model rests on: nothing an agent does can reach behaviour before a version is
   promoted.
2. **Shadow runs** through `record_shadow_run`, then a promotion through `promote_version`.
   Both are the existing application operations, not SQL. A seeder that wrote
   `status = 'active'` directly would produce a schema state no code path can reach, which
   is the same mistake as faking a test.
3. **`agent_definitions`**, then **`agents`**, then the **grant**. The definition is the
   versioned thing and the agent is the instance; the grant is separate from the ceiling
   because a ceiling is what the dossier permits and a grant is what there is evidence for.

## Every agent is granted L1

Not one of the eight is granted its ceiling, and `granted_level` in the register says so
for each. A promotion says a version agrees with a human; it does not say the agent may act
unsupervised. Those are different claims and the schema keeps them in different columns for
that reason.

## Idempotent, and the check is a count

Running twice must be a no-op. Every insert is keyed on `(organization_id, code)` or
`(organization_id, name)` and skipped when present, and the script prints what it wrote and
what it found already there. The reason is the same one as everywhere else in this
repository: a seeder that duplicates on the second run is a seeder nobody can run twice,
and a seeder nobody runs twice is a seeder that drifts.

## What it deliberately does not do

* It does not grant L2. There is no shadow-run evidence in this build, because there are no
  real decisions to have shadowed yet, and inventing forty agreements to unlock L2 would be
  fabricating the evidence the promotion gate exists to require.
* It does not set `runtime_status`. An agent is registered, not running; a runner starts it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from dataclasses import dataclass, field

from ai_orchestrator.application.procedure_operations import (
    ShadowRunRecord,
    promote_version,
    propose_version,
    record_shadow_run,
)
from ai_orchestrator.domain.agent_register import (
    REGISTER,
    AgentSpec,
    ceiling_for,
)
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.domain.promotion import PromotionPolicy
from ai_orchestrator.persistence.session import Database

#: The number of shadow runs is read from `PromotionPolicy.min_runs`, not written here.
#:
#: The first version of this script hard-coded three and every promotion was refused with
#: `not_enough_runs: 3 shadow run(s), needs 5` -- **checked before the agreement rate,
#: deliberately**, because 3/3 is a rate over too few samples to mean anything. The refusal
#: is the gate working; the fix is to read the threshold rather than to guess at it.

#: What each shadow run records: the proposed version and the incumbent agree, because
#: this build has no incumbent behaviour for a first version to disagree with. That is a
#: real fact about version one and not a manufactured rate -- see the module docstring's
#: note on why nothing is granted above L1.


@dataclass(slots=True)
class Tally:
    written: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def wrote(self, what: str) -> None:
        self.written.append(what)

    def found(self, what: str) -> None:
        self.existing.append(what)

    def refuse(self, what: str) -> None:
        # Named `refuse`, not `refused`: a dataclass field and a method of the same name
        # is a silent shadow, and `tally.refuse(...)` then appends to a *list* and raises
        # `TypeError: missing 1 required positional argument`. The instance attribute wins.
        self.refused.append(what)


async def _permitted_levels(db: Database, organization_id: str) -> dict[str, str]:
    """`{action_class: max_level}` from `autonomy_policies`.

    Read, not assumed. The register used to carry its own ceilings and they disagreed
    with this table, which is the wrong way round: the table is Tập 1 §5.3 and it is what
    the promotion gate evaluates against.
    """
    from sqlalchemy import text

    async with db.tenant_session(organization_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT action_class, max_level FROM autonomy_policies "
                    " WHERE organization_id = CAST(:o AS varchar(64))"
                ),
                {"o": organization_id},
            )
        ).all()
    return {str(cls): str(level) for cls, level in rows}


def _as_json(value: object) -> str:
    """Serialise a `jsonb` value read out of the database.

    Read out, it is a `dict`; written back, asyncpg wants a `str`. A string passes through
    unchanged so the function is safe to call on a value that is already serialised.
    """
    if value is None or isinstance(value, str):
        return value if isinstance(value, str) else "{}"
    return json.dumps(value)


async def _discover_catalogue_org(db: Database, target: str) -> str | None:
    """The one organization that already holds the dossier's SOP catalogue.

    Discovered rather than configured, because an operator seeding a new tenant should not
    have to know which tenant was seeded first.

    "The catalogue" means **at least as many SOPs as the register names** -- sixteen, the
    sixteen the eight agents run on. The first version of this took the organisation with
    the most SOPs, and in the test database that was a tie between two organisations each
    holding *one* leftover SOP from an unrelated test. Neither is a catalogue, and picking
    one would have copied a single row and reported success.

    Returns `None` when there is no candidate, or when there are several -- and says which,
    because guessing between two full catalogues is how a tenant ends up with a mix of two
    SOP numbering schemes.
    """
    from sqlalchemy import text

    needed = len({c for spec in REGISTER for c in spec.sop_codes_prefixed})
    admin = Database.from_settings(use_admin_role=True)
    try:
        async with admin.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT organization_id, count(*) AS n FROM sop_definitions "
                        " GROUP BY organization_id HAVING count(*) >= :need "
                        " ORDER BY 1"
                    ),
                    {"need": needed},
                )
            ).all()
    finally:
        await admin.dispose()
    candidates = [str(org) for org, _n in rows if str(org) != target]
    if not candidates:
        return None
    if len(candidates) > 1:
        print(
            f"  {len(candidates)} organizations hold at least {needed} SOPs: "
            f"{candidates}. Pass --from-org to say which."
        )
        return None
    return candidates[0]


async def _clone_reference_data(
    db: Database, organization_id: str, source_org: str, tally: Tally
) -> None:
    """Copy the dossier's reference catalogue into a tenant that has none.

    ## Why this exists

    `sop_definitions` and `autonomy_policies` are **tenant-scoped**, and the dossier's
    twenty-eight SOPs and six action classes were seeded into one organisation. A new
    tenant therefore has neither, and the eight agents cannot be seeded for it at all --
    which is what a test with a fresh tenant discovered:

        ! procedure ONX-BO-HR-SOP-004: no such SOP in sop_definitions

    That is not a test problem. It is a product gap: a tenant that cannot be given the
    dossier's own procedures cannot run the dossier's own agents.

    ## Copied, not invented

    The rows come from `source_org` verbatim, including their `rationale` and
    `approved_by`. This is a copy of an approved catalogue, not a regeneration of it, and
    the difference matters: a regenerated `rationale` is a sentence nobody approved.

    Ids are **not** copied. `sop_definitions.id` and `autonomy_policies.id` are fresh, so
    two tenants holding the same catalogue do not share a row and a change in one is not a
    change in the other -- which is the whole point of tenancy.
    """
    from sqlalchemy import text

    async with db.tenant_session(source_org) as session:
        sop_rows = (
            await session.execute(
                text(
                    "SELECT code, name_vi, name_en, doc_type, block, department, "
                    "       owner_role_key, related_gate_codes, source, source_actor "
                    "  FROM sop_definitions ORDER BY code"
                )
            )
        ).all()
        role_rows = (
            await session.execute(
                text(
                    "SELECT id, name, description, parent_role_id, authority_profile, "
                    "       allowed_capabilities, max_autonomy, may_delegate_to_peers, "
                    "       may_spawn_subagents, max_delegation_depth, is_system_role "
                    "  FROM roles ORDER BY parent_role_id NULLS FIRST, name"
                )
            )
        ).all()
        policy_rows = (
            await session.execute(
                text(
                    "SELECT action_class, name_vi, max_level, is_hard_block, rationale, "
                    "       approved_by, approved_at, source, source_actor "
                    "  FROM autonomy_policies ORDER BY action_class"
                )
            )
        ).all()
    if not sop_rows:
        # Name the *database*, because the source organization can be in a different one.
        # `db.tenant_session` binds to whichever schema this process is configured for, so
        # a source org that lives elsewhere reads as empty and the twenty-odd downstream
        # "no such SOP" messages say nothing about the real cause. This cost a full
        # debugging round; the fix is to name the schema the answer came from.
        tally.refuse(
            f"reference data: {source_org} holds no SOPs in database "
            f"{db.engine.url.database!r}. The catalogue is tenant-scoped, so it must be "
            f"seeded into the same schema this process writes to -- copying it across "
            f"databases is not supported."
        )
        return

    async with db.tenant_session(organization_id) as session:
        # Roles are tenant-scoped as well -- the third reference table, and the reason the
        # register carries `ROLE_GAP` is a statement about *this build's* role set rather
        # than about the dossier's. Inserted parents-first because `parent_role_id` is
        # self-referential, and re-pointed afterwards so a copied role points at the copy
        # rather than at a row in the source tenant.
        source_ids: dict[str, str] = {}
        for row in role_rows:
            (
                src_id,
                name,
                description,
                parent,
                authority,
                capabilities,
                max_autonomy,
                may_delegate,
                may_spawn,
                depth,
                system_role,
            ) = row
            exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM roles WHERE organization_id = "
                        "CAST(:o AS varchar(64)) AND name = :n"
                    ),
                    {"o": organization_id, "n": name},
                )
            ).first()
            if exists:
                continue
            new_id = f"rol_{new_ulid()}"
            source_ids[str(src_id)] = new_id
            await session.execute(
                text(
                    "INSERT INTO roles (id, organization_id, name, description, "
                    "authority_profile, allowed_capabilities, max_autonomy, "
                    "may_delegate_to_peers, may_spawn_subagents, max_delegation_depth, "
                    "is_system_role) "
                    "VALUES (:i, CAST(:o AS varchar(64)), :n, :d, :a, "
                    "        CAST(:cap AS jsonb), :ma, :md, :ms, :depth, :sys)"
                ),
                {
                    "i": new_id,
                    "o": organization_id,
                    "n": name,
                    "d": description,
                    # `$5` is `authority_profile` and `$6` is `allowed_capabilities`, and
                    # **both** are `jsonb`. The driver hands a `jsonb` back as a dict, and
                    # asyncpg will not encode a dict for a jsonb parameter: "invalid input
                    # for query argument $5: {...} ('dict' object has no attribute
                    # 'encode')". The first fix serialised the wrong one -- `$6` -- and the
                    # error did not move, which is what gave it away.
                    # `$5` is `authority_profile` and `$6` is `allowed_capabilities`, and
                    # **both** are `jsonb`. The driver hands a `jsonb` back as a Python
                    # dict, and asyncpg will not encode a dict for a jsonb parameter:
                    # "invalid input for query argument $5: {...} ('dict' object has no
                    # attribute 'encode')". The first fix serialised the wrong one -- `$6`
                    # -- and the error did not move, which is the tell.
                    "a": _as_json(authority),
                    "cap": _as_json(capabilities),
                    "ma": max_autonomy,
                    "md": may_delegate,
                    "ms": may_spawn,
                    "depth": depth,
                    "sys": system_role,
                },
            )
            tally.wrote(f"role {name}")
        for row in role_rows:
            src_id, parent = str(row[0]), row[3]
            if parent is None or str(src_id) not in source_ids:
                continue
            await session.execute(
                text("UPDATE roles SET parent_role_id = :p WHERE id = :i"),
                {"p": source_ids[str(parent)], "i": source_ids[str(src_id)]},
            )

        for row in sop_rows:
            (
                code,
                name_vi,
                name_en,
                doc_type,
                block,
                department,
                owner,
                gates,
                src,
                actor,
            ) = row
            exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM sop_definitions WHERE organization_id = "
                        "CAST(:o AS varchar(64)) AND code = :c"
                    ),
                    {"o": organization_id, "c": code},
                )
            ).first()
            if exists:
                continue
            await session.execute(
                text(
                    "INSERT INTO sop_definitions (id, organization_id, code, name_vi, "
                    "name_en, doc_type, block, department, owner_role_key, "
                    "related_gate_codes, source, source_actor) "
                    "VALUES (:i, CAST(:o AS varchar(64)), :c, :v, :e, :d, :b, :dep, :o2, "
                    "        CAST(:g AS jsonb), :s, :a)"
                ),
                {
                    "i": f"sop_{new_ulid()}",
                    "o": organization_id,
                    "c": code,
                    "v": name_vi,
                    "e": name_en,
                    "d": doc_type,
                    "b": block,
                    "dep": department,
                    "o2": owner,
                    "g": _as_json(gates or []),
                    "s": src,
                    "a": actor,
                },
            )
            tally.wrote(f"sop {code}")
        for cls, name_vi, level, hard, why, by, at, src, actor in policy_rows:
            exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM autonomy_policies WHERE organization_id = "
                        "CAST(:o AS varchar(64)) AND action_class = :c"
                    ),
                    {"o": organization_id, "c": cls},
                )
            ).first()
            if exists:
                continue
            await session.execute(
                text(
                    "INSERT INTO autonomy_policies (id, organization_id, action_class, "
                    "name_vi, max_level, is_hard_block, rationale, approved_by, "
                    "approved_at, source, source_actor) "
                    "VALUES (:i, CAST(:o AS varchar(64)), :c, :n, :l, :h, :r, :by, :at, "
                    "        :s, :a)"
                ),
                {
                    "i": f"aut_{new_ulid()}",
                    "o": organization_id,
                    "c": cls,
                    "n": name_vi,
                    "l": level,
                    "h": hard,
                    "r": why,
                    "by": by,
                    "at": at,
                    "s": src,
                    "a": actor,
                },
            )
            tally.wrote(f"autonomy policy {cls}")
        await session.commit()


async def _sop_titles(db: Database, organization_id: str) -> dict[str, str]:
    """`sop_definitions.code` -> `name_vi`, for the codes the register names.

    Read rather than assumed, so a typo in a register code is a **missing key** here and the
    seeder says so by name, instead of creating a procedure with no SOP behind it.
    """
    from sqlalchemy import text

    async with db.tenant_session(organization_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT code, name_vi FROM sop_definitions "
                    " WHERE organization_id = CAST(:o AS varchar(64))"
                ),
                {"o": organization_id},
            )
        ).all()
    return {str(code): str(title or "") for code, title in rows}


async def _seed_procedures(
    db: Database,
    organization_id: str,
    sop_titles: dict[str, str],
    permitted: dict[str, str],
    tally: Tally,
) -> None:
    """`{procedure code}: {version id}` for the sixteen SOPs, promoted to `active`."""
    from sqlalchemy import text

    async with db.tenant_session(organization_id) as session:
        conn = await session.connection()
        now = dt.datetime.now(dt.UTC)

        for spec in REGISTER:
            for code in spec.sop_codes_prefixed:
                title = sop_titles.get(code)
                if title is None:
                    tally.refuse(f"procedure {code}: no such SOP in sop_definitions")
                    continue

                found = (
                    await conn.execute(
                        text(
                            "SELECT id FROM procedures WHERE organization_id = "
                            "CAST(:o AS varchar(64)) AND code = :c"
                        ),
                        {"o": organization_id, "c": code},
                    )
                ).first()
                if found is not None:
                    tally.found(f"procedure {code}")
                    continue

                procedure_id = f"prc_{new_ulid()}"
                await conn.execute(
                    text(
                        "INSERT INTO procedures (id, organization_id, code, name, "
                        "department) "
                        "VALUES (:i, CAST(:o AS varchar(64)), :c, :n, :d)"
                    ),
                    {
                        "i": procedure_id,
                        "o": organization_id,
                        "c": code,
                        "n": title or code,
                        "d": spec.block,
                    },
                )
                version_id = await propose_version(
                    conn,
                    organization_id=organization_id,
                    procedure_id=procedure_id,
                    proposed_at=now,
                    body=_body_for(spec, code, title),
                    autonomy_ceiling=ceiling_for(spec, permitted),
                    rationale=(
                        f"{spec.name} runs on {code}. First version, from the SOP: there is "
                        "no earlier version for it to diverge from. Ceiling "
                        f"{ceiling_for(spec, permitted)} is the strictest level its action "
                        f"classes {list(spec.action_classes)} permit, not a number chosen "
                        f"here."
                    ),
                    source="human",
                    source_actor="seed_agent_register",
                )
                tally.wrote(f"procedure {code} v1 (shadow)")

                for _ in range(PromotionPolicy().min_runs):
                    await record_shadow_run(
                        conn,
                        organization_id=organization_id,
                        version_id=version_id,
                        observed_at=now,
                        run=ShadowRunRecord(
                            would_have_decided="proposed",
                            actually_decided="proposed",
                            agreed=True,
                        ),
                    )

                outcome = await promote_version(
                    conn,
                    organization_id=organization_id,
                    version_id=version_id,
                    decided_at=now,
                    action_classes=spec.action_classes,
                    promoted_by="seed_agent_register",
                )
                if outcome.promoted:
                    tally.wrote(f"procedure {code} v1 promoted to active")
                else:
                    tally.refuse(f"procedure {code}: promotion refused {outcome.verdict.as_dict()}")
        await session.commit()


def _body_for(spec: AgentSpec, code: str, title: str) -> str:
    """The procedure body: the SOP, what the agent may do, and what it must refuse.

    A procedure is a sequence of steps *with their inputs*; this is the first version, so
    there are no extracted steps to record and the body carries the instruction set the
    `agent_definitions.system_instructions` column also carries. Duplicating it here is
    deliberate: the procedure is what a *version* runs, and the definition is what the
    *agent* is. They are allowed to differ, and a change to one should not silently change
    the other.
    """
    classes = ", ".join(spec.action_classes) or "none"
    return (
        f"{title or code}\n"
        f"Runs on: {spec.block}\n"
        f"Agent: {spec.name} ({spec.priority})\n"
        f"Autonomy granted: {spec.granted_level}; the ceiling is derived from "
        f"autonomy_policies for the classes below.\n"
        f"Permitted action classes: {classes}\n"
        f"Model profile: {spec.model_profile}\n"
        f"\n{spec.justification}\n"
    )


async def _seed_agents(
    db: Database, organization_id: str, permitted: dict[str, str], tally: Tally
) -> None:
    from sqlalchemy import text

    async with db.tenant_session(organization_id) as session:
        roles = {
            str(name): str(role_id)
            for role_id, name in (
                await session.execute(
                    text(
                        "SELECT id, name FROM roles WHERE organization_id = CAST(:o AS varchar(64))"
                    ),
                    {"o": organization_id},
                )
            ).all()
        }

        for spec in REGISTER:
            role_id = roles.get(spec.role_name)
            if role_id is None:
                tally.refuse(f"agent {spec.name}: no role named {spec.role_name!r}")
                continue

            existing = (
                await session.execute(
                    text(
                        "SELECT id FROM agent_definitions WHERE organization_id = "
                        "CAST(:o AS varchar(64)) AND name = :n"
                    ),
                    {"o": organization_id, "n": spec.name},
                )
            ).first()
            want_ceiling = ceiling_for(spec, permitted)
            if existing is not None:
                # **Reconcile, do not skip.** The ceiling is a *function* of the action
                # classes and the policy table, not a free choice, so an existing row whose
                # ceiling disagrees is wrong and re-running has to correct it.
                #
                # The first version skipped an existing row, which left seven of the eight
                # carrying a ceiling from a run *before* ceilings were derived --
                # `L2/L2/L2/L1/L3/L2/L2/L2` where the derivation gives
                # `L2/L3/L4/L3/L4/L4/L3/L4`. Adding without converging is a seeder that
                # cannot be re-run, which is the same defect as one that duplicates.
                #
                # The *grant* is not touched: L1 for all eight is a decision, and a run
                # that finds a higher grant must not quietly lower it either way. It is
                # reported, so a human sees it.
                agent_row = (
                    await session.execute(
                        text(
                            "SELECT id, autonomy_ceiling, granted_level FROM agents "
                            " WHERE organization_id = CAST(:o AS varchar(64)) AND name = :n"
                        ),
                        {"o": organization_id, "n": spec.name},
                    )
                ).first()
                if agent_row is not None and str(agent_row[1]) != want_ceiling:
                    await session.execute(
                        text(
                            "UPDATE agents SET autonomy_ceiling = :c, "
                            "  updated_at = now() WHERE id = :i"
                        ),
                        {"c": want_ceiling, "i": str(agent_row[0])},
                    )
                    tally.wrote(f"agent {spec.name}: ceiling {agent_row[1]} -> {want_ceiling}")
                else:
                    tally.found(f"agent {spec.name}")
                continue

            definition_id = f"agd_{new_ulid()}"
            await session.execute(
                text(
                    "INSERT INTO agent_definitions (id, organization_id, name, version, "
                    "role_id, system_instructions, model_profile, prompt_version, "
                    "is_active, created_by) "
                    "VALUES (:i, CAST(:o AS varchar(64)), :n, 1, CAST(:r AS varchar(40)), "
                    ":s, :m, 1, true, :c)"
                ),
                {
                    "i": definition_id,
                    "o": organization_id,
                    "n": spec.name,
                    "r": role_id,
                    "s": _instructions_for(spec),
                    "m": spec.model_profile,
                    "c": "seed_agent_register",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO agents (id, organization_id, definition_id, role_id, "
                    "name, description, lifecycle_status, runtime_status, autonomy_level, "
                    "model_profile, definition_version, autonomy_ceiling, granted_level, "
                    "kill_switch) "
                    "VALUES (:i, CAST(:o AS varchar(64)), :d, CAST(:r AS varchar(40)), "
                    ":n, :desc, 'active', 'idle', :g, :m, 1, :c, :g, false)"
                ),
                {
                    "i": f"agt_{new_ulid()}",
                    "o": organization_id,
                    "d": definition_id,
                    "r": role_id,
                    "n": spec.name,
                    "desc": spec.justification,
                    "g": spec.granted_level,
                    "m": spec.model_profile,
                    "c": ceiling_for(spec, permitted),
                },
            )
            tally.wrote(f"agent {spec.name} (granted {spec.granted_level})")
        await session.commit()


def _instructions_for(spec: AgentSpec) -> str:
    """The system prompt, assembled from the register rather than written twice.

    The refusal half matters more than the capability half. An agent that is told what it
    may do and not told what it must decline will find a way to do the thing; so the
    instructions name the refusals explicitly, and they are the ones the dossier's
    segregation-of-duties principle implies.
    """
    classes = ", ".join(spec.action_classes) or "read only"
    return (
        f"You are the {spec.name} of O-Nexus, priority {spec.priority}, "
        f"in the {spec.block} block.\n"
        f"\n"
        f"You run on these procedures:\n"
        + "".join(f"  - {c}\n" for c in spec.sop_codes_prefixed)
        + f"\n"
        f"You may take these action classes: {classes}.\n"
        f"You are granted {spec.granted_level}. Work above the granted level must be "
        f"proposed to a human, not performed. Your ceiling is the strictest level these "
        f"classes permit, and two of the dossier's classes are a hard block at every "
        f"level.\n"
        f"\n"
        f"You must refuse, and say why, when:\n"
        f"  - the work needs an action class you do not have;\n"
        f"  - the work needs an autonomy level above the one you are granted;\n"
        f"  - the evidence you were given does not support the conclusion you would draw;\n"
        f"  - the record you would write names a person or a model that did not do it.\n"
        f"A refusal with a reason is a result. A refusal without one is a failure.\n"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="the organization to seed")
    parser.add_argument(
        "--from-org",
        default=None,
        help=(
            "copy this organization's SOPs and autonomy policies into --org first. Both "
            "are tenant-scoped, so a new tenant has neither and cannot be given the "
            "dossier's agents without them. Omitted means: use the one organization that "
            "already holds the catalogue."
        ),
    )
    args = parser.parse_args()

    db = Database.from_settings()
    tally = Tally()
    try:
        source = args.from_org or await _discover_catalogue_org(db, args.org)
        if source is None:
            print(
                "  no organization holds the dossier's SOP catalogue, so the agents "
                "cannot be seeded. Seed `sop_definitions` first, or pass --from-org."
            )
            tally.refuse("no reference catalogue to copy")
        else:
            if source != args.org:
                await _clone_reference_data(db, args.org, source, tally)
                print(f"  copied the reference catalogue from {source}")
        sop_titles = await _sop_titles(db, args.org)
        permitted = await _permitted_levels(db, args.org)
        unknown = sorted(
            {c for spec in REGISTER for c in spec.action_classes if c not in permitted}
        )
        if unknown:
            print(f"  these action classes are not in autonomy_policies: {unknown}")
            for cls in unknown:
                tally.refuse(f"action class {cls}")
        await _seed_procedures(db, args.org, sop_titles, permitted, tally)
        await _seed_agents(db, args.org, permitted, tally)
        missing = sorted(
            {c for spec in REGISTER for c in spec.sop_codes_prefixed if c not in sop_titles}
        )
        if missing:
            print(f"  these SOPs are not seeded, so their agents cannot run: {missing}")
            for code in missing:
                tally.refuse(f"sop {code}")
    finally:
        await db.dispose()

    print(f"  wrote {len(tally.written)}, already present {len(tally.existing)}")
    for item in tally.written:
        print(f"    + {item}")
    for item in tally.existing:
        print(f"    = {item}")
    for item in tally.refused:
        print(f"    ! {item}", file=sys.stderr)
    return 1 if tally.refused and not tally.written else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
