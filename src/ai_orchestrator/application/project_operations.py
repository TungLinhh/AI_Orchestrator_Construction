"""Writing the project spine, and refusing the values the corpus cannot settle.

`ingest/project_reader.py` reads. This writes, following `progress_operations.py`
exactly: SQL is module constants, dates are required parameters rather than clock
reads, and every statement casts `:o` because `organization_id` is `varchar(40)` and
an untyped bind makes Postgres deduce `text` from one context and `varchar` from
another.

## The one rule: a header is a reading, and a reading is not a row

`progress_operations.write_reading` re-checks the domain's rules on the way in rather
than trusting the reader, because the reader is one caller and this layer owns the
table. Same here, and for the same reason.

But the project spine is a harder case than a progress row, and the difference is worth
stating. A progress reading either is or is not readable. **A project header is always
readable and almost always ambiguous**, because the corpus spells the same place nine
ways and cuts two package names short. Writing `HÒA THẠNH, XUÂN CẢNH, SÔNG CẦU, PHÚ YÊN`
into `projects.address` would be picking a canonical spelling, and *that is a business
decision made by a text reader on a Tuesday.*

So this module takes the **resolution** as a required argument rather than deriving
it. A caller that has not decided which address is canonical, and has recorded that
decision somewhere, cannot write a project. That is the whole design: the decision
becomes an input with a shape, instead of a silent default.

## What the corpus cannot supply, and is therefore refused

* **A client.** `clients` needs `code` and `name`; the header block carries project,
  address, package and item and *no client at all*. There is no third Hoabinh-style
  key to derive a client from, and inventing one — "the client is the project name" —
  would make a client whose name happens to be a building's. So `client_id` is
  optional here and a project with no client is a real, writable state.
* **A project code.** The sheets carry names, and the codes live in *filenames*
  (`HBG-MCR-MM-01.1.xlsx`, the `2019.04.28 HBG-HBC-BCTT` folder). A code is therefore
  an argument, and `write_project` refuses a header that arrives without one rather
  than inventing a slug from the name.
* **A package that is a prefix of another.** `'Cơ'` and `'Cơ điện'` are both cut-short
  cells. If the caller resolves one of them, that is a decision; if the caller does
  not, this refuses.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.application.ports import ReadConnection
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.ingest.project_reader import (
    HeaderRead,
    SurveyFinding,
)

_INSERT_PROJECT = """
INSERT INTO projects (
    id, organization_id, client_id, code, name, project_type, status,
    contract_value, currency_code, address, timezone, planned_start,
    planned_completion, source, source_actor, created_at, updated_at
)
VALUES (
    :i, CAST(:o AS varchar(40)), :client, :code, :name, CAST(:ptype AS varchar(128)),
    CAST(:status AS varchar(128)), :value, CAST(:currency AS varchar(8)), :address,
    CAST(:tz AS varchar(64)), :start, :finish, CAST(:source AS varchar(128)),
    :actor, CAST(:now AS timestamptz), CAST(:now AS timestamptz)
)
RETURNING id
"""

_SELECT_PROJECT = """
SELECT id, code, name, address, status, client_id, contract_value, currency_code,
       planned_start, planned_completion
FROM projects
WHERE organization_id = CAST(:o AS varchar(40)) AND code = :code
"""

_SELECT_PROJECTS = """
SELECT id, code, name, address, status, client_id, contract_value, currency_code,
       planned_start, planned_completion
FROM projects
WHERE organization_id = CAST(:o AS varchar(40))
ORDER BY name
"""


@dataclass(frozen=True, slots=True)
class AddressResolution:
    """A decision about which spelling is canonical, and what the alternatives were.

    `chosen` is the value that will be written. `seen` is every distinct spelling the
    corpus contains for this project, so the decision is recorded **against its
    alternatives** rather than on its own — a canonical form with no record of what it
    displaced cannot be reviewed, and the next reader will produce a fourth spelling
    and have no way to know it is new.

    `None` for `chosen` is the refusal: the caller has read the disagreements and
    declined to settle them yet, which is a legitimate answer and a different thing
    from not having looked.
    """

    chosen: str | None
    seen: tuple[str, ...] = ()
    #: Who decided, and when. Not optional, and not defaulted: a canonical spelling
    #: with no hand attached to it is the silent decision this module exists to
    #: prevent.
    decided_by: str = ""
    decided_on: dt.datetime | None = None

    @property
    def is_settled(self) -> bool:
        return bool(self.chosen) and bool(self.decided_by) and self.decided_on is not None

    def refusal_reasons(self) -> tuple[str, ...]:
        """Why this resolution is not yet good enough, in words.

        The count is not the output; these sentences are. "2 problems" sends somebody
        to the function; "no canonical form chosen out of 10 spellings" sends them to
        the sheet.
        """
        reasons: list[str] = []
        if not self.chosen:
            reasons.append(
                f"address: {len(self.seen)} spelling(s) seen and none chosen; "
                f"choosing is a business decision, not a reading"
            )
        if not self.decided_by:
            reasons.append(
                "address: a canonical form with nobody attached to it is the silent "
                "decision this service exists to prevent; pass decided_by"
            )
        if self.decided_on is None:
            reasons.append("address: pass decided_on, so the decision has a date")
        if self.chosen and self.seen and self.chosen not in self.seen:
            reasons.append(
                f"address: chosen {self.chosen!r} is not among the {len(self.seen)} "
                f"spellings the corpus contains, so it was invented here rather than "
                f"read from a file"
            )
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """What one project write produced, or the reasons it produced nothing."""

    project_id: str | None = None
    code: str = ""
    refusals: tuple[str, ...] = field(default_factory=tuple)
    #: The address that was written, and the ones that were not. Kept on the outcome
    #: rather than only logged, so a caller can show the decision to whoever made it.
    address_written: str = ""
    address_alternatives: tuple[str, ...] = field(default_factory=tuple)

    @property
    def written(self) -> bool:
        return self.project_id is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "code": self.code,
            "written": self.written,
            "refusals": list(self.refusals),
            "address_written": self.address_written,
            "address_alternatives": list(self.address_alternatives),
        }


async def write_project(
    conn: AsyncConnection,
    *,
    organization_id: str,
    read: HeaderRead,
    code: str,
    address: AddressResolution,
    observed: Mapping[str, Sequence[str]],
    written_on: dt.datetime,
    client_id: str | None = None,
    project_type: str = "construction",
    status: str = "active",
    currency_code: str = "VND",
    timezone_name: str = "Asia/Ho_Chi_Minh",
    contract_value: float | str = 0,
    planned_start: dt.date | None = None,
    planned_completion: dt.date | None = None,
    source: str = "import",
    source_actor: str = "ingest:project_reader",
) -> WriteOutcome:
    """Write one project from one sheet's header, or refuse and say why.

    `code` is required and is not derived. The sheets carry names and the codes live in
    filenames, so deriving a slug from the name would manufacture an identifier the
    business does not use — and `projects.code` is `UNIQUE (organization_id, code)`,
    so a manufactured code that happens to collide would fail on a row nobody can
    explain.

    `address` is a `AddressResolution`, not a string, and the reasons it carries are
    checked before anything is written. See the module docstring for why the decision
    is an input rather than an output.

    **`contract_value` defaults to 0, and 0 means "the corpus does not say".** It is
    `NOT NULL DEFAULT 0` in the schema, and every other construction table treats an
    absent amount that way, so passing `None` would not be "no value" — it would be an
    explicit NULL that overrides the column's own default and the insert fails. The
    real figure arrives with the contract, through the `contracts` table; a project row
    that has not met its contract yet legitimately has none.
    """
    refusals: list[str] = []

    if not read.accepted:
        refusals.append(
            f"project_name: {read.header.project_name!r} is not a usable name, so the "
            f"sheet does not identify a job"
        )
    if not code.strip():
        refusals.append(
            "code: required. The sheets carry names and the codes live in filenames, "
            "so a code cannot be derived from a header without inventing one"
        )
    refusals.extend(address.refusal_reasons())

    # The package is checked against the corpus's own values rather than against a
    # rule of its own, because `'Cơ'` is only recognisable as cut short by comparison
    # with `'Cơ điện khách sạn và nhà phụ trợ'`. That comparison needs the *survey*,
    # so the caller passes it -- which is why `observed` is a required argument rather
    # than something read from the file.
    seen_packages = set(observed.get("package", ()))
    if read.header.package and any(
        other != read.header.package and other.startswith(read.header.package)
        for other in seen_packages
    ):
        refusals.append(
            f"package: {read.header.package!r} is a strict prefix of "
            f"{sorted(o for o in seen_packages if o.startswith(read.header.package))}, "
            f"which is what a cell cut short by its column width looks like. Writing "
            f"it would put a value in a column that cannot be told from a real one"
        )

    if refusals:
        return WriteOutcome(code=code, refusals=tuple(refusals))

    project_id = f"prj_{new_ulid()}"
    written = await conn.execute(
        text(_INSERT_PROJECT),
        {
            "i": project_id,
            "o": organization_id,
            "client": client_id,
            "code": code,
            "name": read.header.project_name,
            "ptype": project_type,
            "status": status,
            "value": contract_value,
            "currency": currency_code,
            "address": address.chosen,
            "tz": timezone_name,
            "start": planned_start,
            "finish": planned_completion,
            "source": source,
            "actor": source_actor,
            "now": written_on,
        },
    )
    return WriteOutcome(
        project_id=written.scalar_one(),
        code=code,
        address_written=address.chosen or "",
        address_alternatives=tuple(s for s in address.seen if s != address.chosen),
    )


def observed_spellings(findings: Iterable[SurveyFinding]) -> dict[str, tuple[str, ...]]:
    """A `survey_project_headers` result as the mapping `write_project` takes.

    One survey, three projects: the caller reads the corpus once and writes every
    project against the same set of spellings, which is the only way the truncation
    check can see that `Cơ` is a prefix of a value in a *different* workbook.
    """
    return {f.field_name: tuple(f.values) for f in findings}


async def project_by_code(
    conn: AsyncConnection, *, organization_id: str, code: str
) -> dict[str, object] | None:
    """One project by its business code, or `None`.

    Separate from `write_project` so a caller can ask before writing — the difference
    between a confirmation dialog and a lost morning, which is why
    `progress_operations.report_exists` exists.
    """
    row = (
        (await conn.execute(text(_SELECT_PROJECT), {"o": organization_id, "code": code}))
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def list_projects(conn: ReadConnection, *, organization_id: str) -> list[dict[str, object]]:
    """Every project in the tenant, by name.

    The first thing any role-shaped read needs, and the reason `projects` having zero
    writers was worth measuring: an endpoint over this table returns an empty list, and
    an empty list is indistinguishable from a broken one unless somebody has counted
    the table.
    """
    rows = (await conn.execute(text(_SELECT_PROJECTS), {"o": organization_id})).mappings().all()
    return [dict(r) for r in rows]


__all__ = [
    "AddressResolution",
    "WriteOutcome",
    "list_projects",
    "observed_spellings",
    "project_by_code",
    "write_project",
]
