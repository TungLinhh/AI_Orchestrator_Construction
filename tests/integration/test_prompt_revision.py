"""Prompt upgrades preserve reviewed tenant edits and versioned history."""

import pytest
from sqlalchemy import select

from ai_orchestrator.application.prompt_revision import prompt_hash, revise_agent_prompt
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError
from ai_orchestrator.persistence.models import AgentDefinition, AuditLog, Execution
from ai_orchestrator.persistence.repositories.organization import (
    AgentDefinitionRepository,
    AgentRepository,
    RoleRepository,
)
from ai_orchestrator.persistence.repositories.task import ExecutionRepository, TaskRepository

pytestmark = pytest.mark.integration
ACTOR = Actor(id="reviewed-prompt-operator", kind=ActorType.SYSTEM)


async def setup_agent(tenant):
    role = await RoleRepository(tenant.session, tenant.organization_id).create(name="HR")
    definition = await AgentDefinitionRepository(tenant.session, tenant.organization_id).create(
        name="HR Agent",
        role_id=role.id,
        system_instructions="My tenant custom instructions",
        behavior_config={"turns": 3},
        allowed_child_roles=["reviewer"],
        model_profile="primary",
    )
    agent = await AgentRepository(tenant.session, tenant.organization_id).create(
        name="HR Agent",
        role_id=role.id,
        definition_id=definition.id,
    )
    return agent, definition


async def revise(tenant, agent, **kwargs):
    return await revise_agent_prompt(
        tenant.session,
        tenant.organization_id,
        agent_id=agent.id,
        instructions="Reviewed HR instructions",
        revision="hr-v2",
        expected_hash=kwargs.pop("expected_hash", prompt_hash("My tenant custom instructions")),
        actor=ACTOR,
        **kwargs,
    )


async def test_dry_run_and_apply_preserve_history_capabilities_and_audit(tenant):
    agent, old = await setup_agent(tenant)
    receipt = await revise(tenant, agent)
    assert receipt["status"] == "planned"
    assert agent.definition_id == old.id
    receipt = await revise(tenant, agent, apply=True)
    new = await tenant.session.get(AgentDefinition, receipt["definition_id"])
    assert new.version == agent.definition_version == 2
    assert old.system_instructions == "My tenant custom instructions"
    assert new.behavior_config == old.behavior_config
    assert new.allowed_child_roles == old.allowed_child_roles
    assert new.model_profile == old.model_profile
    audit = (
        await tenant.session.execute(
            select(AuditLog).where(
                AuditLog.organization_id == tenant.organization_id,
                AuditLog.action == "agent.prompt.revise",
            )
        )
    ).scalar_one()
    assert audit.context["before_sha256"] == prompt_hash(old.system_instructions)
    assert "instructions" not in str(audit.context).lower()
    again = await revise(tenant, agent, apply=True)
    assert again["status"] == "unchanged"
    assert again["definition_id"] == new.id


async def test_stale_hash_preserves_custom_prompt(tenant):
    agent, old = await setup_agent(tenant)
    with pytest.raises(PreconditionError, match="changed since review"):
        await revise(tenant, agent, expected_hash=prompt_hash("old seed"), apply=True)
    assert agent.definition_id == old.id
    assert old.system_instructions == "My tenant custom instructions"


async def test_tenant_scope_refuses_foreign_agent(tenant):
    agent, _ = await setup_agent(tenant)
    with pytest.raises(NotFoundError):
        await revise_agent_prompt(
            tenant.session,
            "foreign-tenant",
            agent_id=agent.id,
            instructions="new",
            revision="v2",
            expected_hash="x",
            actor=ACTOR,
            apply=True,
        )


async def test_active_execution_blocks_pin_change(tenant):
    agent, old = await setup_agent(tenant)
    task = await TaskRepository(tenant.session, tenant.organization_id).create(
        title="HR", goal="HR"
    )
    execution = await ExecutionRepository(tenant.session, tenant.organization_id).start(
        task_id=task.id,
        agent_id=agent.id,
        runtime_adapter="fake",
        model_profile="default",
    )
    assert isinstance(execution, Execution)
    with pytest.raises(PreconditionError, match="active executions"):
        await revise(tenant, agent, apply=True)
    assert agent.definition_id == old.id


async def test_cli_apply_commits_visible_to_fresh_connection(tenant, tmp_path):
    import asyncio
    import json
    import sys

    agent, old = await setup_agent(tenant)
    agent_id, old_id = agent.id, old.id
    await tenant.commit()
    prompt = tmp_path / "reviewed-prompt.txt"
    prompt.write_text("CLI-reviewed HR prompt")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "scripts/revise_agent_prompt.py",
        "--org",
        tenant.organization_id,
        "--agent",
        agent_id,
        "--prompt-file",
        str(prompt),
        "--revision",
        "cli-reviewed-v2",
        "--expected-sha256",
        prompt_hash("My tenant custom instructions"),
        "--apply",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    assert process.returncode == 0, stderr.decode()
    receipt = json.loads(stdout)
    async with tenant.db.tenant_session(tenant.organization_id) as fresh:
        current = await AgentRepository(fresh, tenant.organization_id).get(agent_id)
        definition = await fresh.get(AgentDefinition, current.definition_id)
        assert definition.id == receipt["definition_id"] != old_id
        assert definition.system_instructions == "CLI-reviewed HR prompt"
        assert definition.version == current.definition_version == 2
