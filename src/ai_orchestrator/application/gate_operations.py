"""Writing Gate state: registration, checklist generation, decisions.

`domain/gates.py` decides. This module writes. The split is the one the domain
purity test exists to enforce: a business rule that can reach a database needs a
running system to be tested, and these rules decide whether a project may
proceed.

So the application layer here is deliberately thin. It loads a session, calls a
pure function, and maps the answer to SQL. Every judgement — what blocks a Gate,
what the outcome would be, whether a decision is well-formed — lives in the
domain, where it is tested without a fixture.

Two things this layer does that the domain cannot, and both are worth naming.

**It generates the checklist.** Tập 2 §E.1 step 1: "hệ thống sinh checklist hồ sơ
Entry Criteria theo loại Gate". Generated at registration, not at Gate definition
time, because criteria are revised and a checklist generated once and never
refreshed is a checklist that silently diverges from the Gate it belongs to.

**It requires a timestamp for a decision.** `decided_at` is a parameter with no
default, because the domain refuses to read the clock — the purity test allows
`datetime.now()` in exactly one file. A Gate decision's time is part of the audit
record and it is the caller's to supply, which also makes the decision
reproducible in a test.

## Why the SQL is module constants

Every statement below is a plain module-level string, not an f-string. That is a
deliberate shape rather than a style preference, and it was arrived at by being
pushed the other way:

* `organization_id` is `varchar(40)` and the tenant parameter arrives as an
  untyped bind, so Postgres deduces `text` from one context and `varchar` from
  another and refuses the statement as `AmbiguousParameterError: inconsistent
  types deduced for parameter $1` — naming neither the column nor the cause.
  Every query therefore needs an explicit `CAST(:o AS varchar(40))`.
* Spelling that cast inline in six statements is duplication; spelling it as a
  constant and interpolating it makes each statement an f-string, which ruff
  flags as `S608` on a range whose last line moves as the query is edited.
* Naming the whole statement puts the cast in the string, keeps every query
  greppable and reviewable as one unit, and needs no suppression at all.

Which is the general point: a linter suppression is a request for the tool to
leave something alone, and the three costs of this one were a range that moves,
a `noqa` that is sometimes unused, and a reviewer having to verify that the
interpolated values are constants. Constants have none of those.

Transactions are `begin_nested` savepoints rather than a top-level commit, so a
failure between the decision and its conditions leaves a Gate that was not
decided rather than a decision with no conditions — which would read as a pass.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.domain import gates as G
from ai_orchestrator.domain.ids import new_ulid

_LOAD_SESSION = """
SELECT i.id, i.organization_id, g.code AS gate_code, g.chair_role_key,
       g.lead_time_days, g.max_extensions, i.subject_kind, i.subject_id,
       i.status, i.extension_count
FROM gate_instances i
JOIN gate_definitions g ON g.id = i.gate_definition_id
WHERE i.id = :iid
"""

_LOAD_EVALUATIONS = """
SELECT e.id, e.state, e.evidence_ref, e.assessed_by, e.assessed_at,
       e.waiver_note, e.waived_by, c.code AS criterion_code,
       c.title AS criterion_title, c.criterion_type, c.is_mandatory, c.weight
FROM gate_criterion_evaluations e
JOIN gate_criteria c ON c.id = e.gate_criterion_id
WHERE e.gate_instance_id = :iid
ORDER BY c.criterion_type, c.code
"""

_INSERT_INSTANCE = """
INSERT INTO gate_instances (id, organization_id, gate_definition_id, subject_kind,
                            subject_id, status, registered_at, scheduled_for,
                            source, source_actor)
VALUES (:i, CAST(:o AS varchar(40)), :g, :sk, :si, 'in_session', now(), :when,
        'human', CAST(:actor AS varchar(255)))
"""

#: Entry criteria only. Exit criteria describe the *next* Gate's readiness and
#: are checked when this Gate is decided; putting them in front of an operator
#: now would be lines that cannot be satisfied until after the decision they are
#: part of.
_GENERATE_CHECKLIST = """
INSERT INTO gate_criterion_evaluations (id, organization_id, gate_instance_id,
                                        gate_criterion_id, state, source, source_actor)
SELECT gen_random_uuid()::text, CAST(:o AS varchar(40)), :i, c.id, 'not_applicable',
       'human', CAST(:actor AS varchar(255))
FROM gate_criteria c
WHERE c.organization_id = :o AND c.gate_definition_id = :g
  AND c.criterion_type = 'entry'
"""

_INSERT_DECISION = """
INSERT INTO gate_decisions (id, organization_id, gate_instance_id, outcome,
                            decided_at, approved_by, compiled_by, attendee_count,
                            quorum, rationale, source, source_actor)
VALUES (:i, CAST(:o AS varchar(40)), :g, :outcome, :when, :by, :compiled, :count,
        :quorum, :rationale, 'human', CAST(:actor AS varchar(255)))
"""

_INSERT_CONDITION = """
INSERT INTO gate_conditions (id, organization_id, gate_decision_id, description,
                             owner_role_key, owner_person, due_on, status,
                             source, source_actor)
VALUES (:i, CAST(:o AS varchar(40)), :d, :desc, :role, :person, :due, 'open',
        'human', CAST(:actor AS varchar(255)))
"""

_UPDATE_INSTANCE_STATUS = """
UPDATE gate_instances SET status = :status, decided_at = :when WHERE id = :i
"""

_EXTEND_WITHIN_LIMIT = """
UPDATE gate_instances SET extension_count = :n, status = 'conditional' WHERE id = :i
"""

_EXTEND_OVER_LIMIT = """
UPDATE gate_instances SET extension_count = :n, status = 'on_hold', decided_at = :when
WHERE id = :i
"""


async def _rows(
    conn: AsyncConnection, sql: str, params: dict[str, object]
) -> list[dict[str, object]]:
    result = await conn.execute(text(sql), params)
    return [dict(row) for row in result.mappings().all()]


def _require_int(value: object, field_name: str) -> int:
    if value is None:
        msg = f"{field_name} is NULL; the row is incomplete"
        raise ValueError(msg)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{field_name} is {value!r}, expected an integer"
        raise TypeError(msg)
    return value


def _optional_int(value: object, field_name: str) -> int | None:
    return None if value is None else _require_int(value, field_name)


async def load_session(conn: AsyncConnection, instance_id: str) -> G.GateSession:
    """Load a Gate instance and its answered checklist.

    Two queries rather than a join, because the checklist is generated at
    registration and may legitimately be empty for an instance registered before
    its Gate's criteria were configured. A join would drop the instance in that
    case and report "no such Gate" for a Gate that exists.
    """
    head = await _rows(conn, _LOAD_SESSION, {"iid": instance_id})
    if not head:
        msg = f"no gate instance {instance_id!r}"
        raise LookupError(msg)
    row = head[0]
    evaluations = await _rows(conn, _LOAD_EVALUATIONS, {"iid": instance_id})

    return G.GateSession(
        instance_id=instance_id,
        organization_id=str(row["organization_id"]),
        gate_code=str(row["gate_code"]),
        subject_kind=str(row["subject_kind"]),
        subject_id=str(row["subject_id"]),
        status=str(row["status"]),
        # The casts are conversions plus checks. A NULL where a number is
        # required means the instance was written without its Gate definition,
        # which deserves a name rather than a `None` reaching an arithmetic
        # comparison and quietly answering the wrong question.
        extension_count=_require_int(row["extension_count"], "extension_count"),
        max_extensions=_require_int(row["max_extensions"], "max_extensions"),
        chair_role_key=str(row["chair_role_key"]),
        lead_time_days=_optional_int(row["lead_time_days"], "lead_time_days"),
        evaluations=evaluations,
    )


async def register_session(
    conn: AsyncConnection,
    *,
    organization_id: str,
    gate_definition_id: str,
    subject_kind: str,
    subject_id: str,
    actor: str,
    scheduled_for: dt.date | None = None,
) -> str:
    """Open a Gate session and generate its entry checklist.

    Tập 2 §E.1 step 1. L4 in the dossier's terms: checklist generation, not a
    judgement. The generated rows are *unanswered* — `not_applicable`, which the
    domain counts as blocking — because a checklist that arrived pre-ticked would
    let a Gate pass by never being read.
    """
    instance_id = f"gin_{new_ulid()}"
    async with conn.begin_nested():
        await conn.execute(
            text(_INSERT_INSTANCE),
            {
                "i": instance_id,
                "o": organization_id,
                "g": gate_definition_id,
                "sk": subject_kind,
                "si": subject_id,
                "when": scheduled_for,
                "actor": actor,
            },
        )
        await conn.execute(
            text(_GENERATE_CHECKLIST),
            {
                "o": organization_id,
                "i": instance_id,
                "g": gate_definition_id,
                "actor": actor,
            },
        )
    return instance_id


async def record_decision(
    conn: AsyncConnection,
    *,
    session: G.GateSession,
    outcome: str,
    approved_by: str,
    rationale: str,
    decided_at: dt.datetime,
    compiled_by: str = "",
    attendee_count: int = 0,
    quorum: int = 0,
    conditions: tuple[dict[str, object], ...] = (),
) -> str:
    """Record a Gate decision, its conditions, and the instance's new status.

    Validation is the domain's; this is the mapping. The refusals happen in
    `G.validate_decision` before anything is written, so a malformed decision
    leaves no partial state.

    The write order matters: the decision first, then the conditions hanging off
    it, inside one savepoint — because a condition with no decision is an orphan
    nobody escalates, and a decision with no conditions reads as a pass.

    `decided_at` is required rather than defaulted, so the domain does not have to
    read the clock and the decision is reproducible in a test.
    """
    G.validate_decision(
        outcome=outcome,
        rationale=rationale,
        approved_by=approved_by,
        compiled_by=compiled_by,
        conditions=conditions,
    )
    decision_id = f"gdc_{new_ulid()}"
    status = G.next_instance_status(outcome)

    async with conn.begin_nested():
        await conn.execute(
            text(_INSERT_DECISION),
            {
                "i": decision_id,
                "o": session.organization_id,
                "g": session.instance_id,
                "outcome": outcome,
                "when": decided_at,
                "by": approved_by,
                "compiled": compiled_by,
                "count": attendee_count,
                "quorum": quorum,
                "rationale": rationale,
                "actor": approved_by,
            },
        )
        for condition in conditions:
            await conn.execute(
                text(_INSERT_CONDITION),
                {
                    "i": f"gco_{new_ulid()}",
                    "o": session.organization_id,
                    "d": decision_id,
                    "desc": condition["description"],
                    "role": condition["owner_role_key"],
                    "person": condition.get("owner_person", ""),
                    "due": condition["due_on"],
                    "actor": approved_by,
                },
            )
        await conn.execute(
            text(_UPDATE_INSTANCE_STATUS),
            {"status": status, "when": decided_at, "i": session.instance_id},
        )
    return decision_id


async def extend_conditional_pass(
    conn: AsyncConnection, *, session: G.GateSession, decided_at: dt.datetime
) -> str:
    """Record another extension of a conditional pass, and convert if it is the last.

    Tập 2 §E.1 step 5: extended more than twice, a conditional pass becomes a HOLD
    and reports to the CEO. The count is incremented and the limit checked in the
    same transaction, so two concurrent extensions cannot both read the same count
    and each conclude it was within the limit.

    Returns the resulting status, so the caller knows whether to escalate. The
    CEO notification itself is not here: this layer writes rows, and a send is
    neither a row nor silent. The `on_hold` status plus the incremented count is
    the durable signal the escalation workflow polls.
    """
    new_count = session.extension_count + 1
    if new_count <= session.max_extensions:
        await conn.execute(text(_EXTEND_WITHIN_LIMIT), {"n": new_count, "i": session.instance_id})
        return "conditional"
    await conn.execute(
        text(_EXTEND_OVER_LIMIT),
        {"n": new_count, "when": decided_at, "i": session.instance_id},
    )
    return "on_hold"


__all__ = [
    "extend_conditional_pass",
    "load_session",
    "record_decision",
    "register_session",
]
