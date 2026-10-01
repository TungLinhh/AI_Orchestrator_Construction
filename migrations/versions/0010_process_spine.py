"""The process spine: Gates G0-G5, SOPs, delegation of authority, autonomy policy.

Tập 1 calls this "xương sống quản trị" and it is the last of the data written and
the first of the system that matters. Every table here exists to turn a rule that
was previously prose into something the database will refuse.

Three things in the source dossier corrected an earlier plan, and the corrections
matter because each was a plausible mistake.

**The Gates are not where I had them.** Tập 1 §2.2:

| Gate | Name | Handover |
|---|---|---|
| G0 | Go/No-Go | Front → Front |
| G1 | Bid submission | Front → CEO/PMO |
| G2 | Contract handover | Front → Middle (PM) |
| G3 | **Design freeze / major PO** | Middle (Design) → Middle (PM/Proc) |
| G4 | **Progressive acceptance / T&C** | Middle (PM) → QA/QC → Client |
| G5 | **Handover, final account, close** | Middle → Back (Finance) → PMO |

My plan had G3 as progress control, G4 as practical completion and G5 as
post-completion claims — off by one position across the back half of the spine.
It survived my own review because it read plausibly, and was found only by
reading the document rather than by reasoning about construction projects.

**A Gate has four outcomes.** `PASS`, `PASS_WITH_CONDITIONS`, `HOLD`, `FAIL`. The
second is a pass that creates obligations, and Tập 2 §E.1 step 5 gives it an
automatic consequence: a conditional pass extended more than twice converts itself
to HOLD and reports to the CEO. That rule is only expressible because the decision,
its conditions and the extension count are all rows.

**The autonomy level cannot be overridden by a prompt.** Tập 1 §5.3 says so in as
many words — the level is "mã hóa cứng ở tầng Harness". So it is not a field on an
agent and not something a workflow reads from a prompt: it is a row in
`autonomy_policies`, keyed on an action class, with a ceiling the harness reads
*before* a workflow runs. `sop_steps.autonomy_level` is what the SOP *claims*;
`autonomy_policies.max_level` is what is *permitted*; only the policy is
authoritative. That is the difference between a rule and a suggestion.

My earlier "L0" was the right instinct under the wrong name. Tập 1 calls it "Vùng
cấm tuyệt đối" and lists four absolutely forbidden zones: personnel decisions,
commitments beyond the DOA limit, **safety conclusions**, and any transaction with
a flagged supplier. Those are not a level at all — they stop the action before a
level is considered, which is why they are `is_hard_block` here.

**An agent is never assigned an approval role.** Tập 3 §4.1, verbatim. `sop_raci`
refuses an `A` on a row whose `role_kind` is `agent`, and that single constraint is
what stops the platform growing a route by which a model approves its own work.

**Segregation of duties is enforced, not documented.** Tập 1 §1.2 separates
proposer, reviewer, approver and payer. `gate_decisions` refuses a decision whose
approver compiled the pack.

The table bodies are Alembic's own output, generated rather than retyped, so this
migration and `persistence/process.py` are the same schema by construction rather
than by review. Regenerate rather than edit.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration creates, derived from the file rather than typed
#: in, so "protected" and "created" cannot drift apart. Every one of these is
#: organisation-scoped, including the ones that do not look like business tables
#: such as a controlled vocabulary: a policy on those is meaningful too.
TENANT_TABLES = (
    "autonomy_policies",
    "doa_matrix",
    "gate_conditions",
    "gate_criteria",
    "gate_criterion_evaluations",
    "gate_decisions",
    "gate_definitions",
    "gate_instances",
    "sop_definitions",
    "sop_forms",
    "sop_raci",
    "sop_steps",
    "sop_versions",

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
    op.create_table('autonomy_policies',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('action_class', sa.String(length=128), nullable=False),
    sa.Column('name_vi', sa.String(length=255), server_default='', nullable=False),
    sa.Column('max_level', sa.String(length=128), server_default='L1', nullable=False),
    sa.Column('is_hard_block', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('rationale', sa.Text(), server_default='', nullable=False),
    sa.Column('approved_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_autonomy_policies_proposal_required_for_agent_source')),
    sa.CheckConstraint("NOT (is_hard_block AND max_level <> 'L1') OR max_level = 'L1'", name=op.f('ck_autonomy_policies_a_hard_block_reports_at_l1')),
    sa.CheckConstraint("is_hard_block OR max_level <> ''", name=op.f('ck_autonomy_policies_max_level_present')),
    sa.CheckConstraint("max_level IN ('L1','L2','L3','L4')", name=op.f('ck_autonomy_policies_max_level_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_autonomy_policies_source_known')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_autonomy_policies'))
    )
    op.create_index('uq_autonomy_policies_org_action_class', 'autonomy_policies', ['organization_id', 'action_class'], unique=True)
    op.create_table('doa_matrix',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name_vi', sa.String(length=255), nullable=False),
    sa.Column('subject_kind', sa.String(length=128), server_default='payment', nullable=False),
    sa.Column('min_amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('max_amount', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('approver_role_key', sa.String(length=128), nullable=False),
    sa.Column('fallback_role_key', sa.String(length=128), server_default='', nullable=False),
    sa.Column('max_agent_autonomy', sa.String(length=128), server_default='L3', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_doa_matrix_proposal_required_for_agent_source')),
    sa.CheckConstraint("max_agent_autonomy IN ('L1','L2','L3','L4')", name=op.f('ck_doa_matrix_autonomy_level_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_doa_matrix_source_known')),
    sa.CheckConstraint('max_amount IS NULL OR max_amount >= min_amount', name=op.f('ck_doa_matrix_band_is_not_inverted')),
    sa.CheckConstraint('min_amount >= 0', name=op.f('ck_doa_matrix_min_amount_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_doa_matrix'))
    )
    op.create_index('ix_doa_matrix_org_subject_band', 'doa_matrix', ['organization_id', 'subject_kind', 'min_amount'], unique=False)
    op.create_index('uq_doa_matrix_org_code', 'doa_matrix', ['organization_id', 'code'], unique=True)
    op.create_table('gate_definitions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('name_vi', sa.String(length=255), nullable=False),
    sa.Column('name_en', sa.String(length=255), server_default='', nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('chair_role_key', sa.String(length=128), nullable=False),
    sa.Column('member_role_keys', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('entry_summary', sa.Text(), server_default='', nullable=False),
    sa.Column('exit_summary', sa.Text(), server_default='', nullable=False),
    sa.Column('convened_within', sa.String(length=128), server_default='', nullable=False),
    sa.Column('lead_time_days', sa.Integer(), nullable=True),
    sa.Column('max_extensions', sa.Integer(), server_default='2', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_gate_definitions_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_gate_definitions_source_known')),
    sa.CheckConstraint('lead_time_days IS NULL OR lead_time_days >= 0', name=op.f('ck_gate_definitions_lead_time_non_negative')),
    sa.CheckConstraint('max_extensions >= 0', name=op.f('ck_gate_definitions_max_extensions_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gate_definitions'))
    )
    op.create_index('uq_gate_definitions_org_code', 'gate_definitions', ['organization_id', 'code'], unique=True)
    op.create_table('sop_definitions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name_vi', sa.String(length=255), nullable=False),
    sa.Column('name_en', sa.String(length=255), server_default='', nullable=False),
    sa.Column('doc_type', sa.String(length=128), server_default='SOP', nullable=False),
    sa.Column('block', sa.String(length=128), nullable=False),
    sa.Column('department', sa.String(length=128), nullable=False),
    sa.Column('owner_role_key', sa.String(length=128), nullable=False),
    sa.Column('related_gate_codes', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_sop_definitions_proposal_required_for_agent_source')),
    sa.CheckConstraint("block IN ('FO','MO','BO','PMO')", name=op.f('ck_sop_definitions_block_known')),
    sa.CheckConstraint("code ~ '^ONX-(FO|MO|BO|PMO)-[A-Z]{2,4}-(SOP|WI|FRM|POL|REG)-[0-9]{3}$'", name=op.f('ck_sop_definitions_code_matches_the_dossier_scheme')),
    sa.CheckConstraint("doc_type IN ('SOP','WI','FRM','POL','REG')", name=op.f('ck_sop_definitions_doc_type_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_sop_definitions_source_known')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sop_definitions'))
    )
    op.create_index('uq_sop_definitions_org_code', 'sop_definitions', ['organization_id', 'code'], unique=True)
    op.create_table('gate_criteria',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('gate_definition_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('criterion_type', sa.String(length=128), server_default='entry', nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('detail', sa.Text(), server_default='', nullable=False),
    sa.Column('is_mandatory', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('weight', sa.Integer(), nullable=True),
    sa.Column('required_document_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('waiver_role_key', sa.String(length=128), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_gate_criteria_proposal_required_for_agent_source')),
    sa.CheckConstraint("criterion_type IN ('entry','exit')", name=op.f('ck_gate_criteria_criterion_type_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_gate_criteria_source_known')),
    sa.CheckConstraint('weight IS NULL OR weight > 0', name=op.f('ck_gate_criteria_weight_positive')),
    sa.ForeignKeyConstraint(['gate_definition_id'], ['gate_definitions.id'], name=op.f('fk_gate_criteria_gate_definition_id_gate_definitions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gate_criteria'))
    )
    op.create_index('ix_gate_criteria_org_gate_type', 'gate_criteria', ['organization_id', 'gate_definition_id', 'criterion_type', 'code'], unique=False)
    op.create_index('uq_gate_criteria_org_gate_code', 'gate_criteria', ['organization_id', 'gate_definition_id', 'code'], unique=True)
    op.create_table('gate_instances',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('gate_definition_id', sa.String(length=40), nullable=False),
    sa.Column('subject_kind', sa.String(length=128), server_default='project', nullable=False),
    sa.Column('subject_id', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=128), server_default='not_started', nullable=False),
    sa.Column('registered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scheduled_for', sa.Date(), nullable=True),
    sa.Column('pre_read_issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('extension_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_gate_instances_proposal_required_for_agent_source')),
    sa.CheckConstraint("(status IN ('decided','passed','conditional','on_hold','failed')) = (decided_at IS NOT NULL)", name=op.f('ck_gate_instances_a_decided_gate_has_a_decision_time')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_gate_instances_source_known')),
    sa.CheckConstraint("status IN ('not_started','scheduled','in_session','decided','passed','conditional','on_hold','failed')", name=op.f('ck_gate_instances_status_known')),
    sa.CheckConstraint("subject_kind IN ('opportunity','project')", name=op.f('ck_gate_instances_subject_kind_known')),
    sa.CheckConstraint('extension_count >= 0', name=op.f('ck_gate_instances_extension_count_non_negative')),
    sa.ForeignKeyConstraint(['gate_definition_id'], ['gate_definitions.id'], name=op.f('fk_gate_instances_gate_definition_id_gate_definitions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gate_instances'))
    )
    op.create_index('ix_gate_instances_org_status', 'gate_instances', ['organization_id', 'status'], unique=False)
    op.create_index('ix_gate_instances_org_subject', 'gate_instances', ['organization_id', 'subject_kind', 'subject_id'], unique=False)
    op.create_table('gate_criterion_evaluations',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('gate_instance_id', sa.String(length=40), nullable=False),
    sa.Column('gate_criterion_id', sa.String(length=40), nullable=False),
    sa.Column('state', sa.String(length=128), server_default='not_applicable', nullable=False),
    sa.Column('evidence_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('assessed_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('assessed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('waiver_note', sa.Text(), server_default='', nullable=False),
    sa.Column('waived_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_gate_criterion_evaluations_proposal_required_for_agent_source')),
    sa.CheckConstraint("(state = 'waived') = (waived_by <> '')", name=op.f('ck_gate_criterion_evaluations_a_waiver_names_who_granted_it')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_gate_criterion_evaluations_source_known')),
    sa.CheckConstraint("state <> 'waived' OR length(waiver_note) > 0", name=op.f('ck_gate_criterion_evaluations_a_waiver_states_why')),
    sa.CheckConstraint("state IN ('met','not_met','waived','not_applicable')", name=op.f('ck_gate_criterion_evaluations_state_known')),
    sa.ForeignKeyConstraint(['gate_criterion_id'], ['gate_criteria.id'], name=op.f('fk_gate_criterion_evaluations_gate_criterion_id_gate_criteria')),
    sa.ForeignKeyConstraint(['gate_instance_id'], ['gate_instances.id'], name=op.f('fk_gate_criterion_evaluations_gate_instance_id_gate_instances')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gate_criterion_evaluations'))
    )
    op.create_index('ix_gate_criterion_evaluations_org_state', 'gate_criterion_evaluations', ['organization_id', 'gate_instance_id', 'state'], unique=False)
    op.create_index('uq_gate_criterion_evaluations_org_instance_criterion', 'gate_criterion_evaluations', ['organization_id', 'gate_instance_id', 'gate_criterion_id'], unique=True)
    op.create_table('gate_decisions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('gate_instance_id', sa.String(length=40), nullable=False),
    sa.Column('outcome', sa.String(length=128), server_default='PASS', nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('approved_by', sa.String(length=128), nullable=False),
    sa.Column('compiled_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('attendee_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('quorum', sa.Integer(), server_default='0', nullable=False),
    sa.Column('rationale', sa.Text(), server_default='', nullable=False),
    sa.Column('minutes_document_id', sa.String(length=40), nullable=True),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_gate_decisions_proposal_required_for_agent_source')),
    sa.CheckConstraint("compiled_by = '' OR compiled_by <> approved_by", name=op.f('ck_gate_decisions_the_compiler_does_not_approve')),
    sa.CheckConstraint("outcome IN ('PASS','PASS_WITH_CONDITIONS','HOLD','FAIL')", name=op.f('ck_gate_decisions_outcome_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_gate_decisions_source_known')),
    sa.CheckConstraint('length(rationale) > 0', name=op.f('ck_gate_decisions_a_decision_states_its_reason')),
    sa.ForeignKeyConstraint(['gate_instance_id'], ['gate_instances.id'], name=op.f('fk_gate_decisions_gate_instance_id_gate_instances')),
    sa.ForeignKeyConstraint(['minutes_document_id'], ['documents.id'], name=op.f('fk_gate_decisions_minutes_document_id_documents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gate_decisions'))
    )
    op.create_index('ix_gate_decisions_org_outcome', 'gate_decisions', ['organization_id', 'outcome'], unique=False)
    op.create_index('uq_gate_decisions_org_instance', 'gate_decisions', ['organization_id', 'gate_instance_id'], unique=True)
    op.create_table('sop_versions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('sop_definition_id', sa.String(length=40), nullable=False),
    sa.Column('major', sa.Integer(), server_default='1', nullable=False),
    sa.Column('minor', sa.Integer(), server_default='0', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='draft', nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('effective_from', sa.Date(), nullable=True),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.Column('document_id', sa.String(length=40), nullable=True),
    sa.Column('drafted_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('reviewed_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('approved_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_sop_versions_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_sop_versions_source_known')),
    sa.CheckConstraint("status <> 'issued' OR approved_by <> ''", name=op.f('ck_sop_versions_an_issued_sop_is_approved')),
    sa.CheckConstraint("status IN ('draft','in_review','issued','superseded','withdrawn')", name=op.f('ck_sop_versions_status_known')),
    sa.CheckConstraint('major >= 0 AND minor >= 0', name=op.f('ck_sop_versions_version_non_negative')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_sop_versions_document_id_documents')),
    sa.ForeignKeyConstraint(['sop_definition_id'], ['sop_definitions.id'], name=op.f('fk_sop_versions_sop_definition_id_sop_definitions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sop_versions'))
    )
    op.create_index('ix_sop_versions_org_status', 'sop_versions', ['organization_id', 'status'], unique=False)
    op.create_index('uq_sop_versions_org_sop_major_minor', 'sop_versions', ['organization_id', 'sop_definition_id', 'major', 'minor'], unique=True)
    op.create_table('gate_conditions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('gate_decision_id', sa.String(length=40), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('owner_role_key', sa.String(length=128), nullable=False),
    sa.Column('owner_person', sa.String(length=128), server_default='', nullable=False),
    sa.Column('due_on', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=128), server_default='open', nullable=False),
    sa.Column('remediated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('evidence_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_gate_conditions_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_gate_conditions_source_known')),
    sa.CheckConstraint("status <> 'remediated' OR remediated_at IS NOT NULL", name=op.f('ck_gate_conditions_a_remediated_condition_says_when')),
    sa.CheckConstraint("status IN ('open','in_progress','remediated','waived','overdue')", name=op.f('ck_gate_conditions_status_known')),
    sa.ForeignKeyConstraint(['gate_decision_id'], ['gate_decisions.id'], name=op.f('fk_gate_conditions_gate_decision_id_gate_decisions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gate_conditions'))
    )
    op.create_index('ix_gate_conditions_org_decision', 'gate_conditions', ['organization_id', 'gate_decision_id'], unique=False)
    op.create_index('ix_gate_conditions_org_due', 'gate_conditions', ['organization_id', 'status', 'due_on'], unique=False)
    op.create_table('sop_steps',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('sop_version_id', sa.String(length=40), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('step_input', sa.Text(), server_default='', nullable=False),
    sa.Column('action', sa.Text(), server_default='', nullable=False),
    sa.Column('step_output', sa.Text(), server_default='', nullable=False),
    sa.Column('sla_text', sa.String(length=128), server_default='', nullable=False),
    sa.Column('sla_hours', sa.Integer(), nullable=True),
    sa.Column('system_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('autonomy_level', sa.String(length=128), server_default='L1', nullable=False),
    sa.Column('action_class', sa.String(length=128), server_default='inform', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_sop_steps_proposal_required_for_agent_source')),
    sa.CheckConstraint("autonomy_level IN ('L1','L2','L3','L4')", name=op.f('ck_sop_steps_autonomy_level_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_sop_steps_source_known')),
    sa.CheckConstraint('sla_hours IS NULL OR sla_hours >= 0', name=op.f('ck_sop_steps_sla_non_negative')),
    sa.ForeignKeyConstraint(['sop_version_id'], ['sop_versions.id'], name=op.f('fk_sop_steps_sop_version_id_sop_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sop_steps'))
    )
    op.create_index('uq_sop_steps_org_version_sequence', 'sop_steps', ['organization_id', 'sop_version_id', 'sequence'], unique=True)
    op.create_table('sop_forms',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name_vi', sa.String(length=255), nullable=False),
    sa.Column('name_en', sa.String(length=255), server_default='', nullable=False),
    sa.Column('sop_step_id', sa.String(length=40), nullable=True),
    sa.Column('retention_years', sa.Integer(), nullable=True),
    sa.Column('is_mandatory', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_sop_forms_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_sop_forms_source_known')),
    sa.CheckConstraint('retention_years IS NULL OR retention_years >= 0', name=op.f('ck_sop_forms_retention_non_negative')),
    sa.ForeignKeyConstraint(['sop_step_id'], ['sop_steps.id'], name=op.f('fk_sop_forms_sop_step_id_sop_steps')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sop_forms'))
    )
    op.create_index('uq_sop_forms_org_code', 'sop_forms', ['organization_id', 'code'], unique=True)
    op.create_table('sop_raci',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('sop_step_id', sa.String(length=40), nullable=False),
    sa.Column('role_key', sa.String(length=128), nullable=False),
    sa.Column('role_kind', sa.String(length=128), server_default='person', nullable=False),
    sa.Column('letter', sa.String(length=128), nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_sop_raci_proposal_required_for_agent_source')),
    sa.CheckConstraint("NOT (role_kind = 'agent' AND letter = 'A')", name=op.f('ck_sop_raci_an_agent_is_never_accountable')),
    sa.CheckConstraint("letter IN ('R','A','C','I')", name=op.f('ck_sop_raci_letter_known')),
    sa.CheckConstraint("role_kind IN ('person','committee','agent')", name=op.f('ck_sop_raci_role_kind_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_sop_raci_source_known')),
    sa.ForeignKeyConstraint(['sop_step_id'], ['sop_steps.id'], name=op.f('fk_sop_raci_sop_step_id_sop_steps')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sop_raci'))
    )
    op.create_index('uq_sop_raci_org_step_role', 'sop_raci', ['organization_id', 'sop_step_id', 'role_key'], unique=True)



    # RLS last, so every table exists before any policy is created.
    # `transaction_per_migration` is on in `migrations/env.py`, so this either
    # fully applies or fully rolls back; a half-protected tranche is not a
    # reachable state.
    for _table in TENANT_TABLES:
        _protect(_table)


def downgrade() -> None:
    # `DROP TABLE` takes its policies with it, so there is nothing to unprotect
    # explicitly. The drop order below is Alembic's own reverse-dependency
    # order, so children go before parents.

    op.drop_index('uq_sop_raci_org_step_role', table_name='sop_raci')
    op.drop_table('sop_raci')
    op.drop_index('uq_sop_forms_org_code', table_name='sop_forms')
    op.drop_table('sop_forms')
    op.drop_index('uq_sop_steps_org_version_sequence', table_name='sop_steps')
    op.drop_table('sop_steps')
    op.drop_index('ix_gate_conditions_org_due', table_name='gate_conditions')
    op.drop_index('ix_gate_conditions_org_decision', table_name='gate_conditions')
    op.drop_table('gate_conditions')
    op.drop_index('uq_sop_versions_org_sop_major_minor', table_name='sop_versions')
    op.drop_index('ix_sop_versions_org_status', table_name='sop_versions')
    op.drop_table('sop_versions')
    op.drop_index('uq_gate_decisions_org_instance', table_name='gate_decisions')
    op.drop_index('ix_gate_decisions_org_outcome', table_name='gate_decisions')
    op.drop_table('gate_decisions')
    op.drop_index('uq_gate_criterion_evaluations_org_instance_criterion', table_name='gate_criterion_evaluations')
    op.drop_index('ix_gate_criterion_evaluations_org_state', table_name='gate_criterion_evaluations')
    op.drop_table('gate_criterion_evaluations')
    op.drop_index('ix_gate_instances_org_subject', table_name='gate_instances')
    op.drop_index('ix_gate_instances_org_status', table_name='gate_instances')
    op.drop_table('gate_instances')
    op.drop_index('uq_gate_criteria_org_gate_code', table_name='gate_criteria')
    op.drop_index('ix_gate_criteria_org_gate_type', table_name='gate_criteria')
    op.drop_table('gate_criteria')
    op.drop_index('uq_sop_definitions_org_code', table_name='sop_definitions')
    op.drop_table('sop_definitions')
    op.drop_index('uq_gate_definitions_org_code', table_name='gate_definitions')
    op.drop_table('gate_definitions')
    op.drop_index('uq_doa_matrix_org_code', table_name='doa_matrix')
    op.drop_index('ix_doa_matrix_org_subject_band', table_name='doa_matrix')
    op.drop_table('doa_matrix')
    op.drop_index('uq_autonomy_policies_org_action_class', table_name='autonomy_policies')
    op.drop_table('autonomy_policies')

