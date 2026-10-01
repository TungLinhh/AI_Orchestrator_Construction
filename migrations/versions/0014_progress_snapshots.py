"""Construction progress: the plan and its outcome on one row.

One table, `progress_snapshots`, two supporting indexes, and two composite foreign
keys. It answers the question `docs/PRODUCT_GAP.md` §4a left open: whether the
corpus's `KH` / `TT` column pairs are a new table, an extension of
`material_reconciliations`, or something else.

**They are their own table, and planned and actual share a row.** The sheets pair
them column for column — planned start, actual start, planned finish, actual
finish, planned duration, actual duration — and the question the sheet exists to
answer is the difference between them. Six columns of one row makes that a
subtraction. Splitting them behind a `basis` discriminator would make it a
self-join, and would store the pairing less directly than the file does.

**It is not `material_reconciliations` and must not be merged with it.** That table,
from migration `0012`, models *milestones for materials*: requested, ordered,
expected, delivered, read from the `VẬT TƯ <zone>` sheets. This one models
*durations for construction activities*. Different subject, different question,
different arithmetic. Merging them would put "when did the client ask for it" and
"how long did the pipe-laying take" on the same row and call the result a timeline.

## Why the two supporting indexes exist, and why both directions are hand-ordered

`project_id` and `wbs_item_id` are declared as
`FOREIGN KEY (organization_id, <col>) REFERENCES <table>(organization_id, id)`, not
as a bare reference to `id`.

A bare `ForeignKey("projects.id")` is satisfied by **any** project row in the
database. That is not a leak — RLS stops one tenant reading another's project, so
nothing is disclosed — it is a *corrupt pointer*, which is the harder kind to find:
a progress report scoped to one project that silently contains another tenant's
activities. It was measured rather than assumed; the test that found it is
`TestPointersAreTenantScoped`.

`procurement.py` already established the composite form for `unit_code` and said why
in the column comment: *RLS stops the read; this stops the pointer, which is the
direction that corrupts an estimate.* `uq_projects_org_id` and `uq_wbs_items_org_id`
are what let Postgres accept that form — a composite foreign key needs a unique
constraint on the pair, and `id` alone is not one. They are redundant for uniqueness
(`id` is the primary key) and Postgres will not use them to look anything up.

**Autogenerate gets the order wrong in both directions, and only running it shows
that.** It emits a table's own indexes after the table body, which is right for its
own indexes and wrong for a foreign key's support, because Postgres resolves the
target at `CREATE TABLE` time. Applying the generated order verbatim failed with

    ProgrammingError: there is no unique constraint matching given keys
    for referenced table "projects"

and the reverse is wrong too: the generated `downgrade` drops the supporting indexes
*before* the table, which fails with

    ERROR: cannot drop index uq_wbs_items_org_id because other objects depend on it

So the two `create_index` calls are moved above `create_table` and the two
`drop_index` calls are moved below `drop_table`. Those four line moves are the only
manual edits in this file, and the reason each is there is recorded at the call site
as well as here.

## Two measurements from the only real file, both enforced

`TĐ BOH.xlsx :: TĐ .BOH` is the only progress sheet in the corpus with a KH/TT pair.

**`% Hoàn thành` holds a fraction, not a percentage.** The header reads "percentage
complete" and the cells hold `0.65`, `0.8`, `0.9`, `0`. `completion_ratio` is a ratio
constrained to `0..1`, and `domain/progress.completion_ratio` refuses a value above 1
rather than dividing it by 100. A genuine `65` on a future sheet and a mis-keyed
`0.65` on this one are indistinguishable, and silently choosing between them is how a
completion figure ends up a hundred times out with nothing raised.

The column is `Numeric(7, 4)` rather than the obvious `Numeric(6, 4)` for that
reason: at width 6 a `100` is refused by the *column width* — `NumericValueOutOfRange`
— rather than by the rule. Same outcome, different meaning, and a constraint that
fires for a reason nobody wrote down is one nobody can reason about later.

**`Số ngày` is an inclusive calendar day count.** Five for five:

    2019-03-13 -> 2019-03-14   Số ngày 2    (exclusive: 1)
    2019-04-17 -> 2019-04-26   Số ngày 10   (exclusive: 9)
    2019-04-01 -> 2019-04-20   Số ngày 20   (exclusive: 19)
    2019-06-30 -> 2019-07-30   Số ngày 31   (exclusive: 30)
    2019-08-20 -> 2019-09-03   Số ngày 15   (exclusive: 14)

So `planned_duration_days` must equal `planned_finish_on - planned_start_on + 1`, and
likewise for the actual pair. A schedule counting working days is *refused* rather
than quietly accepted, which is the same trade as `units_dictionary` having no
default: a refusal is recoverable, a duration on the wrong convention is
indistinguishable from a right one everywhere downstream.

## `actual_updated`, and why the corpus makes it necessary

In that same file **every data row has planned dates identical to its actual dates**
— the actual columns were never filled in from the plan. A variance computed from
this corpus is 0 days for every activity, which is not "on time", it is "nobody
recorded the actual". `actual_updated` records the fact that separates them. Nothing
in the schema *forces* it either way, because a genuinely on-time activity has equal
dates too, and a constraint that refused the honest case would push readers to mark
untouched rows as reported.

## `HPNC KH` / `HPNC TT` are stored without a name

Two small integers per row (2, 6, 6, 6, 3), a planned and an actual value, on a
weekly progress sheet. Read alongside the `Tháng` column the candidates are handover,
a week number, or a milestone index, and the corpus does not say which. They are
`handover_planned` and `handover_actual` with the ambiguity recorded in the column
comment. Naming them after a guess would make the guess invisible to the next reader.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration creates, derived from the file rather than typed
#: in, so "protected" and "created" cannot drift apart. Every one of these is
#: organisation-scoped, including the ones that do not look like business tables
#: such as a controlled vocabulary: a policy on those is meaningful too.
TENANT_TABLES = (
    "progress_snapshots",

)

APP_ROLE = "ao_app"


def _protect(table: str) -> None:
    """Enable and force RLS, install the isolation policy, grant DML.

    Identical to what 0002 did for the original 44 tables and what 0007 did for
    the first construction tranche, including the `FORCE`. Without `FORCE` the
    owner bypasses the policy, and the owner is the role that ran this migration.
    """
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" '
        "USING (organization_id = current_setting('app.current_tenant', true)) "
        "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO {APP_ROLE}')


def upgrade() -> None:
    op.create_index('uq_projects_org_id', 'projects', ['organization_id', 'id'], unique=True)
    op.create_index('uq_wbs_items_org_id', 'wbs_items', ['organization_id', 'id'], unique=True)
    op.create_table('progress_snapshots',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('report_ref', sa.String(length=128), nullable=False),
    sa.Column('line_label', sa.String(length=128), server_default='', nullable=False),
    sa.Column('line_no', sa.Integer(), server_default='0', nullable=False),
    sa.Column('section_label', sa.Text(), server_default='', nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=True),
    sa.Column('wbs_item_id', sa.String(length=40), nullable=True),
    sa.Column('item_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('work_description', sa.Text(), server_default='', nullable=False),
    sa.Column('system_code', sa.String(length=4), nullable=True),
    sa.Column('zone_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('period_label', sa.String(length=128), server_default='', nullable=False),
    sa.Column('observed_on', sa.Date(), nullable=True),
    sa.Column('planned_start_on', sa.Date(), nullable=True),
    sa.Column('planned_finish_on', sa.Date(), nullable=True),
    sa.Column('planned_duration_days', sa.Integer(), nullable=True),
    sa.Column('actual_start_on', sa.Date(), nullable=True),
    sa.Column('actual_finish_on', sa.Date(), nullable=True),
    sa.Column('actual_duration_days', sa.Integer(), nullable=True),
    sa.Column('actual_updated', sa.Boolean(), nullable=True),
    sa.Column('completion_ratio', sa.Numeric(precision=7, scale=4), nullable=True),
    sa.Column('item_completion_ratio', sa.Numeric(precision=7, scale=4), nullable=True),
    sa.Column('status_text', sa.String(length=128), server_default='', nullable=False),
    sa.Column('is_adequate', sa.Boolean(), nullable=True),
    sa.Column('handover_planned', sa.Integer(), nullable=True),
    sa.Column('handover_actual', sa.Integer(), nullable=True),
    sa.Column('engineer_comment', sa.Text(), server_default='', nullable=False),
    sa.Column('drawing_name', sa.String(length=128), server_default='', nullable=False),
    sa.Column('raw_cells', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_progress_snapshots_agent_source_needs_proposal')),
    sa.CheckConstraint("is_adequate IS NULL OR lower(status_text) IN ('yes','no','co','có','khong','không','y','n')", name=op.f('ck_progress_snapshots_status_flag_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_progress_snapshots_source_known')),
    sa.CheckConstraint("system_code IS NULL OR system_code IN ('PW','LV','WD','AC','FP')", name=op.f('ck_progress_snapshots_system_code_known')),
    sa.CheckConstraint("work_description <> '' OR planned_start_on IS NOT NULL OR actual_start_on IS NOT NULL OR completion_ratio IS NOT NULL", name=op.f('ck_progress_snapshots_row_is_an_observation')),
    sa.CheckConstraint('actual_duration_days IS NULL OR actual_duration_days >= 1', name=op.f('ck_progress_snapshots_actual_days_positive')),
    sa.CheckConstraint('actual_duration_days IS NULL OR actual_start_on IS NULL OR actual_finish_on IS NULL OR actual_duration_days = actual_finish_on - actual_start_on + 1', name=op.f('ck_progress_snapshots_actual_days_match_dates')),
    sa.CheckConstraint('actual_finish_on IS NULL OR actual_start_on IS NOT NULL', name=op.f('ck_progress_snapshots_actual_finish_needs_a_start')),
    sa.CheckConstraint('actual_finish_on IS NULL OR actual_start_on IS NULL OR actual_finish_on >= actual_start_on', name=op.f('ck_progress_snapshots_actual_window_ordered')),
    sa.CheckConstraint('completion_ratio IS NULL OR (completion_ratio >= 0 AND completion_ratio <= 1)', name=op.f('ck_progress_snapshots_completion_in_range')),
    sa.CheckConstraint('handover_actual IS NULL OR handover_actual >= 0', name=op.f('ck_progress_snapshots_handover_actual_non_negative')),
    sa.CheckConstraint('handover_planned IS NULL OR handover_planned >= 0', name=op.f('ck_progress_snapshots_handover_planned_non_negative')),
    sa.CheckConstraint('item_completion_ratio IS NULL OR (item_completion_ratio >= 0 AND item_completion_ratio <= 1)', name=op.f('ck_progress_snapshots_item_completion_in_range')),
    sa.CheckConstraint('planned_duration_days IS NULL OR planned_duration_days >= 1', name=op.f('ck_progress_snapshots_planned_days_positive')),
    sa.CheckConstraint('planned_duration_days IS NULL OR planned_start_on IS NULL OR planned_finish_on IS NULL OR planned_duration_days = planned_finish_on - planned_start_on + 1', name=op.f('ck_progress_snapshots_planned_days_match_dates')),
    sa.CheckConstraint('planned_finish_on IS NULL OR planned_start_on IS NOT NULL', name=op.f('ck_progress_snapshots_planned_finish_needs_a_start')),
    sa.CheckConstraint('planned_finish_on IS NULL OR planned_start_on IS NULL OR planned_finish_on >= planned_start_on', name=op.f('ck_progress_snapshots_planned_window_ordered')),
    sa.ForeignKeyConstraint(['organization_id', 'project_id'], ['projects.organization_id', 'projects.id'], name=op.f('fk_progress_snapshots_organization_id_projects')),
    sa.ForeignKeyConstraint(['organization_id', 'wbs_item_id'], ['wbs_items.organization_id', 'wbs_items.id'], name=op.f('fk_progress_snapshots_organization_id_wbs_items')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_progress_snapshots'))
    )
    op.create_index('ix_progress_snapshots_org_late', 'progress_snapshots', ['organization_id', 'project_id', 'planned_finish_on', 'actual_finish_on'], unique=False)
    op.create_index('ix_progress_snapshots_org_project_observed', 'progress_snapshots', ['organization_id', 'project_id', 'observed_on'], unique=False)
    op.create_index('ix_progress_snapshots_org_wbs_item', 'progress_snapshots', ['organization_id', 'wbs_item_id'], unique=False)
    op.create_index('uq_progress_snapshots_org_report_line', 'progress_snapshots', ['organization_id', 'report_ref', 'line_label'], unique=True)



    # RLS last, so every table exists before any policy is created.
    # `transaction_per_migration` is on in `migrations/env.py`, so this either
    # fully applies or fully rolls back; a half-protected tranche is not a
    # reachable state.
    for _table in TENANT_TABLES:
        _protect(_table)


def downgrade() -> None:
    # `DROP TABLE` takes its policies with it, so there is nothing to unprotect
    # explicitly. The drop order below is Alembic's own reverse-dependency
    # order, so children go before parents -- with one exception this file fixes
    # by hand, recorded in the module docstring: the two supporting indexes are
    # dropped *after* the table, not before it. The composite foreign keys depend
    # on them, and Postgres refuses to drop an index another object relies on:
    #
    #     ERROR: cannot drop index uq_wbs_items_org_id because other objects
    #            depend on it
    #
    # which is only visible by running the downgrade, since the upgrade's order is
    # fine and autogenerate has no way to know which index a foreign key needs.

    op.drop_index('uq_progress_snapshots_org_report_line', table_name='progress_snapshots')
    op.drop_index('ix_progress_snapshots_org_wbs_item', table_name='progress_snapshots')
    op.drop_index('ix_progress_snapshots_org_project_observed', table_name='progress_snapshots')
    op.drop_index('ix_progress_snapshots_org_late', table_name='progress_snapshots')
    op.drop_table('progress_snapshots')

    op.drop_index('uq_wbs_items_org_id', table_name='wbs_items')
    op.drop_index('uq_projects_org_id', table_name='projects')

