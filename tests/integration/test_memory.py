"""Memory isolation and retrieval quality, against a real pgvector index.

The cross-tenant test here is the one that matters most in this file. A vector
search that filters by tenant after ranking leaks: HNSW is approximate, so on a
tight similarity budget a neighbouring row from another organisation can be
returned. The filter therefore has to be in the query, and the test has to prove
it by putting two tenants' vectors close together in the same index.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from ai_orchestrator.domain.contracts import MemoryScope
from ai_orchestrator.domain.enums import DataClassification, MemoryTier
from ai_orchestrator.domain.errors import ValidationError
from ai_orchestrator.domain.ids import OrganizationId
from ai_orchestrator.memory.service import (
    CHUNK_CHARS,
    HashEmbedder,
    MemoryService,
)
from ai_orchestrator.persistence.models import MemoryChunk
from ai_orchestrator.persistence.session import Database

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def org_ids() -> tuple[str, str]:
    admin = Database.from_settings(use_admin_role=True)
    made = []
    for _ in range(2):
        org_id = str(OrganizationId.create())
        async with admin.session() as session:
            await session.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:i, :s, 'memory test')"),
                {"i": org_id, "s": f"mem-{org_id[-8:]}"},
            )
            await session.commit()
        made.append(org_id)
    await admin.dispose()
    return made[0], made[1]


@pytest_asyncio.fixture
async def service(db: Database, org_ids: tuple[str, str]):
    """A `MemoryService` bound to the first organization.

    The transaction is owned by the fixture rather than by a context manager, so
    a test that has to write rows which a later statement must see can commit
    without tearing the fixture down.
    """
    org_id, _other = org_ids
    session = db.session_factory()
    await session.begin()
    await session.execute(
        text("SELECT set_config('app.current_tenant', :org, true)"), {"org": org_id}
    )
    try:
        yield MemoryService(session, org_id, embedder=HashEmbedder())
    finally:
        await session.rollback()
        await session.close()


def _scope(org_id: str, **overrides: object) -> MemoryScope:
    defaults: dict[str, object] = {
        "organization_id": OrganizationId(org_id),
        "max_classification": DataClassification.RESTRICTED,
    }
    return MemoryScope(**(defaults | overrides))


class TestWriteAndChunk:
    async def test_writes_item_and_chunks(self, service: MemoryService) -> None:
        content = "Revenue was up. " * 400
        result = await service.write(content=content, tier=MemoryTier.SEMANTIC, title="long note")
        assert result.chunk_count > 1, "a long document must be chunked"
        assert result.embedded
        item = await service.get(result.item_id)
        assert item.tier == "semantic"
        assert item.content_hash

    async def test_chunks_respect_the_size_limit(self, service: MemoryService) -> None:
        result = await service.write(content="\n\n".join(["x" * 900] * 10))
        for index in range(result.chunk_count):
            chunk = await service._session.scalar(
                select(MemoryChunk.content).where(
                    MemoryChunk.memory_item_id == result.item_id,
                    MemoryChunk.chunk_index == index,
                )
            )
            assert len(chunk) <= CHUNK_CHARS

    async def test_empty_content_is_refused(self, service: MemoryService) -> None:
        with pytest.raises(ValidationError):
            await service.write(content="   ")

    async def test_identical_content_is_still_separate_items(self, service: MemoryService) -> None:
        """Deduplicating memory on content hash would lose the fact that two
        different events produced the same text, which is often the signal."""
        first = await service.write(content="same text")
        second = await service.write(content="same text")
        assert first.item_id != second.item_id


class TestRetrievalQuality:
    async def test_finds_the_relevant_chunk(self, service: MemoryService) -> None:
        await service.write(
            content="The retention rate dropped to 94% in the enterprise segment.",
            title="retention",
            source_type="document",
            source_ref="doc://retention-report",
        )
        await service.write(
            content="The office was repainted in a pale green during the summer.",
            title="facilities",
            source_type="document",
            source_ref="doc://facilities",
        )
        scope = _scope(str(await _org_of(service)))
        results = await service.search("retention rate enterprise", scope)
        assert results
        assert "retention" in results[0].content
        assert results[0].score > 0

    async def test_provenance_is_reported(self, service: MemoryService) -> None:
        """A memory with no source is returned but marked uncitable, so the model
        can be told the difference between a fact and an agent's own summary."""
        await service.write(
            content="I believe the market will grow.",
            source_type="agent",
        )
        scope = _scope(str(await _org_of(service)))
        results = await service.search("market will grow", scope)
        assert results
        assert results[0].has_provenance is False
        assert results[0].to_citation()["source_type"] == "agent"

    async def test_sourced_memory_is_citable(self, service: MemoryService) -> None:
        await service.write(
            content="Net revenue retention was 118%.",
            source_type="document",
            source_ref="doc://q3-financials",
            confidence=0.95,
        )
        scope = _scope(str(await _org_of(service)))
        results = await service.search("net revenue retention", scope)
        assert results[0].has_provenance is True
        assert results[0].confidence == pytest.approx(0.95)

    async def test_empty_query_is_refused(self, service: MemoryService) -> None:
        with pytest.raises(ValidationError):
            await service.search("", _scope(str(await _org_of(service))))


class TestScopeIsAuthorization:
    async def test_another_tenants_memory_is_never_returned(
        self, db: Database, service: MemoryService, org_ids: tuple[str, str]
    ) -> None:
        """Acceptance criterion: an agent in org A cannot semantic-search org B.

        The two corpora are deliberately identical, because that is the case where
        an approximate index returns a cross-tenant neighbour: the two embeddings
        are the same vector, so a post-ranking filter would let org B's row win
        the ordering. If the tenant predicate is not in the query, this test finds
        it.
        """
        org_a, org_b = org_ids
        secret = "The unreleased product is codenamed Project Nightingale."
        await service.write(
            content=secret, title="org A", source_type="document", source_ref="doc://a"
        )

        async with db.tenant_session(org_b) as other_session:
            other = MemoryService(other_session, org_b, embedder=HashEmbedder())
            await other.write(
                content=secret, title="org B", source_type="document", source_ref="doc://b"
            )
            await other_session.commit()

        results = await service.search("codenamed Project Nightingale", _scope(org_a))
        assert results, "org A must find its own memory"
        sources = {r.source_ref for r in results}
        assert "doc://b" not in sources, "org B's memory leaked into org A's search"
        assert "doc://a" in sources

    async def test_classification_ceiling_is_enforced(self, service: MemoryService) -> None:
        await service.write(
            content="Board discussion: we are considering a takeover.",
            classification=DataClassification.RESTRICTED,
        )
        await service.write(
            content="The office was repainted in a pale green during the summer.",
            classification=DataClassification.INTERNAL,
        )
        org_id = str(await _org_of(service))
        internal_only = await service.search(
            "takeover board discussion",
            _scope(org_id, max_classification=DataClassification.INTERNAL),
        )
        assert all(r.classification is not DataClassification.RESTRICTED for r in internal_only), (
            "a restricted memory was returned to an internal-only request"
        )

    async def test_agent_scoped_memory_is_invisible_to_another_agent(
        self, service: MemoryService
    ) -> None:
        """Agent-scoped memory is private to that agent. Real agent rows are used
        because the foreign key enforces that a memory cannot name an agent that
        does not exist — an orphan scope would be invisible and un-auditable."""
        from ai_orchestrator.persistence.repositories.organization import (
            AgentDefinitionRepository,
            AgentRepository,
            RoleRepository,
        )

        org_id = str(await _org_of(service))
        roles = RoleRepository(service._session, org_id)
        role = await roles.create(name="Worker")
        definitions = AgentDefinitionRepository(service._session, org_id)
        definition = await definitions.create(
            name="worker", role_id=role.id, system_instructions="You are a worker."
        )
        agents = AgentRepository(service._session, org_id)
        agent_a = await agents.create(name="A", role_id=role.id, definition_id=definition.id)
        agent_b = await agents.create(name="B", role_id=role.id, definition_id=definition.id)

        # Commit so the memory write below can reference a committed agent, then
        # re-bind the tenant. The re-bind is not optional: after a commit the
        # `app.current_tenant` GUC is gone, and the next INSERT is refused by the
        # row-level-security policy. That refusal is the isolation guarantee
        # working, not a bug to be worked around.
        await service._session.commit()
        await service._session.execute(
            text("SELECT set_config('app.current_tenant', :org, true)"), {"org": org_id}
        )

        await service.write(content="Agent A private note about the budget.", agent_id=agent_a.id)
        as_b = await service.search("private note budget", _scope(org_id, agent_ids=(agent_b.id,)))
        assert all("Agent A private" not in r.content for r in as_b)

        as_a = await service.search("private note budget", _scope(org_id, agent_ids=(agent_a.id,)))
        assert any("Agent A private" in r.content for r in as_a)

    async def test_org_wide_memory_is_visible_to_everyone(self, service: MemoryService) -> None:
        """An item with no owner is organisation knowledge, not private.

        An agent that names no scope still sees it, because a request with no
        agent_ids is a request for organisation-wide knowledge rather than a
        request for another agent's private notes.
        """
        await service.write(content="The fiscal year ends on 31 March.")
        org_id = str(await _org_of(service))
        results = await service.search("fiscal year ends", _scope(org_id, agent_ids=()))
        assert any("31 March" in r.content for r in results)

    async def test_result_limit_is_bounded_by_the_scope(self, service: MemoryService) -> None:
        for i in range(10):
            await service.write(content=f"Fact number {i} about quarterly revenue growth.")
        org_id = str(await _org_of(service))
        results = await service.search("quarterly revenue growth", _scope(org_id, max_results=3))
        assert len(results) <= 3


class TestLifecycle:
    async def test_soft_delete_removes_chunks(self, service: MemoryService) -> None:
        """A right-to-delete that leaves the embeddings behind has not been
        honoured."""
        result = await service.write(content="Delete me: sensitive content.")
        item_id = result.item_id
        await service.soft_delete(item_id)
        remaining = await service._session.scalar(
            select(func.count())
            .select_from(MemoryChunk)
            .where(MemoryChunk.memory_item_id == item_id)
        )
        assert remaining == 0
        item = await service.get(item_id)
        assert item.is_deleted is True

    async def test_deleted_memory_is_not_retrievable(self, service: MemoryService) -> None:
        result = await service.write(content="Temporary note about the merger.")
        await service.soft_delete(result.item_id)
        org_id = str(await _org_of(service))
        results = await service.search("temporary note merger", _scope(org_id))
        assert all(r.item_id != result.item_id for r in results)

    async def test_stats_group_by_tier(self, service: MemoryService) -> None:
        await service.write(content="Working memory item.", tier=MemoryTier.WORKING)
        await service.write(content="Semantic memory item.", tier=MemoryTier.SEMANTIC)
        stats = await service.stats()
        assert stats.get("working") == 1
        assert stats.get("semantic") == 1


class TestWithoutAnEmbedder:
    async def test_lexical_fallback_still_works(self, db: Database, org_ids) -> None:
        """With no embedder configured, retrieval must degrade to lexical overlap
        rather than silently returning everything."""
        org_id, _ = org_ids
        async with db.tenant_session(org_id) as session:
            plain = MemoryService(session, org_id, embedder=None)
            await plain.write(content="Quarterly revenue rose twelve percent.")
            await plain.write(content="The canteen menu changed on Monday.")
            results = await plain.search("quarterly revenue", _scope(org_id))
            assert results
            assert "revenue" in results[0].content


async def _org_of(service: MemoryService) -> str:
    return service._org
