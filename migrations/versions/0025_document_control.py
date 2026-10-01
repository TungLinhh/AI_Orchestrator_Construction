"""Document control: a code, a version lineage, a distribution matrix, and an expiry.

Revision ID: 0025
Revises: 0024

## What this adds, and why each piece is separate

`sop_definitions` was a catalogue with a version table bolted on and nothing else: no code
beyond a flat string, no way to say which version a reader must have, and no way to record
that a document was **distributed** to anybody. Three additions, three different questions:

1. **`documents.code`** — a document the dossier numbers, named by the scheme. Parsed by
   `domain/document_code.py`; nullable, because `documents` also holds an uploaded
   attachment that has no dossier number and inventing one would be a lie.

2. **`document_versions`** — the lineage. `sop_versions` already versions the *procedure*;
   this versions the *content*, and the two are separate because a procedure can be
   revised without the document changing and the reverse. A single merged table would make
   "which version of the SOP" and "which version of the file" the same question, and they
   are not.

3. **`document_distributions`** — the matrix. Who must have which version, by what channel,
   and whether they acknowledged it. This is the table that turns "the SOP says" into
   something checkable, and its absence is why a controlled document set is a paper exercise
   in most construction companies.

## Expiry is not retention, and the difference is the point of this migration

`documents.retention_until` already exists and means *"how long we must keep this"*. Adding
`expires_on` means something else and neither implies the other:

| | `expires_on` | `retention_until` |
|---|---|---|
| question | is this document still *valid*? | how long must we *keep* it? |
| who sets it | the document's owner | records management / the law |
| when it passes | the document is withdrawn from circulation | the document is archived or destroyed |
| a null means | no expiry — permanent | keep forever, or policy not set |

A **permit** is the case that makes the distinction unavoidable: a construction permit has
a three-year life and a seven-year retention obligation. It expires long before it may be
destroyed, and a schema with one date has to record either the wrong one. The `CHECK` below
refuses the combination that is almost always a mistake — an expiry *after* the retention
deadline means the document is still in force when it must be destroyed, which nobody
decided on purpose.

## Tenant keys are composite from the start

Every foreign key below is `(organization_id, <column>)` referencing
`(organization_id, id)`, and every table carries `UNIQUE (organization_id, id)`.

This is what migrations `0020` through `0023` retrofitted across 135 keys, and it cost two
ORM model files to do (F133). Writing them bare and converting later is the same work with
the same risk, so the composite form is used here as the *starting* shape.

## `document_code` is parsed, and the database agrees

`documents.code` carries a `CHECK` requiring the five-segment shape. Postgres cannot parse
the scheme, so the check is the shape — `ONX-BO-HR-SOP-004` and `ONX-BO-HRS-SOP-004` both
satisfy it, and `domain/document_code.py` is what decides whether the block and the kind
are real. The `CHECK` is a cheap guard against a hand-typed string, not the grammar; saying
so here means nobody later mistakes it for the grammar.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHORT = sa.String(length=128)
NAME = sa.String(length=255)
CODE = sa.String(length=32)
ID = sa.String(length=40)
LONG = sa.Text()

#: The shape `domain/document_code.py` enforces, as far as SQL can. A cheap guard against a
#: hand-typed string -- **not** the grammar, which lives in Python and is what decides
#: whether the block and the kind exist.
APP_ROLE = "ao_app"


def _protect(table: str) -> None:
    """Enable and force RLS, install the isolation policy, grant DML.

    Verbatim from `0010_process_spine.py`, which took it from `0007` and `0002`. The
    `FORCE` is the part that is easy to leave out and the part that matters: without it
    the *owner* bypasses the policy, and the owner is the role that runs this migration --
    so a table created without it looks protected in every test that connects as the
    application role and is open to any process holding the owner's password.

    The first version of this migration created two tenant-scoped tables and **did not
    call this at all**. `alembic check` reported no drift, the model reconciled, every
    constraint test passed, and the tables had `relrowsecurity = false` and zero policies
    while their ninety-five siblings had `true` and one. The only reason it was found is
    that a test counted rows and got three instead of one: with no policy there is nothing
    to scope the count, so the query saw rows from a previous run.

    A schema that claims tenancy needs a check that says so per table, not a convention --
    the same argument as the provenance constraints, and the same failure mode.
    """
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" '
        "USING (organization_id = current_setting('app.current_tenant', true)) "
        "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


CODE_SHAPE = r"^ONX-[A-Z]{2,3}-[A-Z]{2,3}-[A-Z]{2,4}-[0-9]{3}$"


def upgrade() -> None:
    op.add_column("documents", sa.Column("code", CODE, nullable=True))
    op.add_column("documents", sa.Column("expires_on", sa.Date(), nullable=True))
    # The **suffix**, not the full name. Alembic's `create_check_constraint` runs the
    # name through the naming convention, which composes `ck_%(table_name)s_%(constraint_name)s`
    # -- so the full name came out as
    # `ck_documents_ck_documents_code_is_well_formed` and `downgrade` then failed with
    # `UndefinedObjectError: constraint "ck_documents_expiry_precedes_retention" ... does
    # not exist`. Fourth time this session (F138, and the model's own version of it).
    op.create_check_constraint(
        "code_is_well_formed",
        "documents",
        "code IS NULL OR code ~ '" + CODE_SHAPE + "'",
    )
    op.create_index(
        "uq_documents_org_code", "documents", ["organization_id", "code"], unique=True
    )
    # The one combination that is never a decision. See the module docstring.
    op.create_check_constraint(
        "expiry_precedes_retention",
        "documents",
        "expires_on IS NULL OR retention_until IS NULL "
        "OR expires_on <= (retention_until AT TIME ZONE 'UTC')::date",
    )

    op.create_table(
        "document_versions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("organization_id", ID, nullable=False),
        sa.Column("document_id", ID, nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", SHORT, nullable=False, server_default="draft"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("supersedes_id", ID, nullable=True),
        sa.Column("issued_on", sa.Date(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("change_note", NAME, nullable=False, server_default=""),
        sa.Column("approved_by", SHORT, nullable=False, server_default=""),
        sa.Column("approval_id", ID, nullable=True),
        sa.Column(
            "source", SHORT, nullable=False, server_default="human"
        ),
        sa.Column("source_actor", SHORT, nullable=False, server_default=""),
        sa.Column("proposal_id", ID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_document_versions_document",
        ),        sa.UniqueConstraint(
            "organization_id", "document_id", "version_no",
            name="uq_document_versions_number",
        ),
        sa.CheckConstraint("version_no > 0", name="version_positive"),
        sa.CheckConstraint(
            "status IN ('draft','in_review','approved','superseded','withdrawn')",
            name="status_known",
        ),
        # A version that has been superseded must say when it stopped applying, and a
        # version still in force must not have an end date. A version table that permits
        # both at once cannot answer "which version was in force on the tenth".
        sa.CheckConstraint(
            "(status <> 'superseded') OR (effective_to IS NOT NULL)",
            name="a_superseded_version_ends",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL "
            "OR effective_to >= effective_from",
            name="the_window_is_not_inverted",
        ),
        # The provenance rule, as a constraint rather than a convention: a row an agent
        # proposed names its proposal, and a row a person wrote does not claim one. The
        # second half is the one that matters here -- an agent-proposed version carries an
        # `approval_id`, so a proposed version nobody approved cannot be marked `approved`.
        sa.CheckConstraint(
            "((source = 'agent_proposal') = (proposal_id IS NOT NULL))",
            name="agent_source_needs_proposal",
        ),
        sa.CheckConstraint(
            "source <> 'agent_proposal' OR approval_id IS NOT NULL",
            name="agent_needs_approval",
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR issued_on IS NOT NULL",
            name="an_approved_version_is_issued",
        ),
    )
    op.create_index(
        "uq_document_versions_org_id",
        "document_versions",
        ["organization_id", "id"],
        unique=True,
    )
    op.create_index(
        "ix_document_versions_document",
        "document_versions",
        ["organization_id", "document_id", "version_no"],
    )
    op.create_index(
        "ix_document_versions_effective",
        "document_versions",
        ["organization_id", "document_id", "status", "effective_from"],
    )

    op.create_table(
        "document_distributions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("organization_id", ID, nullable=False),
        sa.Column("document_version_id", ID, nullable=False),
        sa.Column("audience_role_key", SHORT, nullable=False),
        sa.Column("channel", SHORT, nullable=False, server_default="system"),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("distributed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", SHORT, nullable=False, server_default=""),
        sa.Column("note", NAME, nullable=False, server_default=""),
        sa.Column("source", SHORT, nullable=False, server_default="human"),
        sa.Column("source_actor", SHORT, nullable=False, server_default=""),
        sa.Column("proposal_id", ID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_version_id"],
            ["document_versions.organization_id", "document_versions.id"],
            name="fk_document_distributions_version",
        ),
        # One row per (version, audience, channel): a re-send is an update of the same
        # obligation, not a second one. Without this, a retried distribution doubles a
        # mandatory count and the compliance figure becomes fiction.
        sa.UniqueConstraint(
            "organization_id", "document_version_id", "audience_role_key", "channel",
            name="uq_document_distributions_target",
        ),
        sa.CheckConstraint(
            "channel IN ('system','email','print','signage','induction')",
            name="channel_known",
        ),
        # The house rule, as constraints. Copied rather than imported so a migration
        # keeps working when the model moves on -- a migration that imported
        # `provenance_checks()` would change behaviour the day that function did.
        sa.CheckConstraint(
            "source IN ('human','agent_proposal','import','system')", name="source_known"
        ),
        sa.CheckConstraint(
            "((source = 'agent_proposal') = (proposal_id IS NOT NULL))",
            name="agent_source_needs_proposal",
        ),
        # An acknowledgement with nobody behind it is a lie, and an acknowledgement before
        # the distribution is a clock fault. Both are cheap to refuse and awkward to notice.
        sa.CheckConstraint(
            "acknowledged_at IS NULL OR (distributed_at IS NOT NULL "
            "AND acknowledged_at >= distributed_at)",
            name="acknowledged_after_distribution",
        ),
        sa.CheckConstraint(
            "acknowledged_at IS NULL OR length(acknowledged_by) > 0",
            name="an_acknowledgement_names_who",
        ),
        sa.CheckConstraint(
            "NOT is_mandatory OR distributed_at IS NOT NULL",
            name="a_mandatory_unsent_row_is_not_a_row",
        ),
    )
    op.create_index(
        "uq_document_distributions_org_id",
        "document_distributions",
        ["organization_id", "id"],
        unique=True,
    )
    op.create_index(
        "ix_document_distributions_audience",
        "document_distributions",
        ["organization_id", "audience_role_key", "acknowledged_at"],
    )
    op.create_index(
        "ix_document_distributions_version",
        "document_distributions",
        ["organization_id", "document_version_id", "is_mandatory"],
    )


    for table in ("document_versions", "document_distributions"):
        _protect(table)


def downgrade() -> None:
    # The policies and the RLS flags go with the tables, so nothing is dropped explicitly:
    # `DROP TABLE` takes the policies with it. What *is* dropped explicitly is the grants,
    # which also go with the table -- and this note exists because a reader will wonder.
    op.drop_table("document_distributions")
    op.drop_table("document_versions")
    op.drop_index("uq_documents_org_code", table_name="documents")
    op.drop_constraint("ck_documents_expiry_precedes_retention", "documents")
    op.drop_constraint("ck_documents_code_is_well_formed", "documents")
    op.drop_column("documents", "expires_on")
    op.drop_column("documents", "code")
