"""Memory: tiers, scoping and retrieval.

The single most important property here is that similarity ranking is not
authorization. A vector search that filters by tenant *after* ranking is a
cross-tenant disclosure waiting for a tight similarity budget, because HNSW is an
approximate index and can return a neighbour from another tenant. So the tenant
predicate is part of the query, and the filter runs in SQL before the ordering.

The second property is that a memory item without provenance is not evidence.
Retrieval returns `source` and `confidence` alongside the text, and the
distinction is the caller's to act on. A platform that returns bare text to a
model has thrown away the only thing that would let the model tell a fact from a
rumour.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.contracts import MemoryScope
from ai_orchestrator.domain.enums import DataClassification, MemoryTier, classification_at_least
from ai_orchestrator.domain.errors import NotFoundError, ValidationError
from ai_orchestrator.domain.ids import MemoryItemId
from ai_orchestrator.models.gateway import cosine_similarity
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import MemoryChunk, MemoryItem

#: Chunk size for a long document. Small enough that a retrieved chunk is a
#: specific answer rather than a whole document, large enough that the embedding
#: still captures the surrounding meaning.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150

#: pgvector's HNSW refuses more than 2000 dimensions. Asserted rather than
#: discovered: the failure arrives at migration time otherwise.
MAX_EMBEDDING_DIMENSIONS = 2000


@dataclass(slots=True)
class RetrievedMemory:
    """One retrieved chunk, with the provenance needed to judge it."""

    item_id: str
    chunk_id: str
    content: str
    score: float
    tier: MemoryTier
    classification: DataClassification
    source_type: str
    source_ref: str | None
    provenance: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    created_at: Any = None

    @property
    def has_provenance(self) -> bool:
        """Whether this memory can be cited as evidence.

        Deliberately strict: a source reference is required, and an item written
        by an agent with no source does not qualify. An agent summarising its own
        reasoning is not a source.
        """
        return bool(self.source_ref) and self.source_type not in {"", "agent"}

    def to_citation(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "score": round(self.score, 4),
            "tier": self.tier.value,
            "classification": self.classification.value,
            "has_provenance": self.has_provenance,
            "confidence": self.confidence,
        }


@dataclass(slots=True)
class MemoryWriteResult:
    item_id: str
    chunk_count: int
    embedded: bool
    skipped_reason: str | None = None


class MemoryService:
    """Write and retrieve memory, always within a scope.

    Takes a session and an organization. Every query filters on
    `organization_id` explicitly, in addition to the RLS policy: the redundancy
    is the point, because a repository that relied on RLS alone would be one
    diagnostic query away from a breach.
    """

    def __init__(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        embedder: Any = None,
        embedding_model: str = "hash-512",
    ) -> None:
        self._session = session
        self._org = organization_id
        self._embedder = embedder
        self._embedding_model = embedding_model

    async def write(
        self,
        *,
        content: str,
        tier: MemoryTier = MemoryTier.WORKING,
        kind: str = "note",
        title: str = "",
        org_unit_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        classification: DataClassification = DataClassification.INTERNAL,
        source_type: str = "agent",
        source_ref: str | None = None,
        provenance: dict[str, Any] | None = None,
        confidence: float | None = None,
        retention_until: Any = None,
        created_by: str | None = None,
    ) -> MemoryWriteResult:
        """Write a memory item and its chunks.

        A memory with no source reference is accepted but recorded as
        `source_type='agent'`, which `RetrievedMemory.has_provenance` will treat
        as uncitable. Refusing outright would push agents to invent a source.
        """
        if not content.strip():
            msg = "memory content must not be empty"
            raise ValidationError(msg, details={"field": "content"})

        item_id = str(MemoryItemId.create())
        item = MemoryItem(
            id=item_id,
            organization_id=self._org,
            org_unit_id=org_unit_id,
            agent_id=agent_id,
            task_id=task_id,
            tier=tier.value,
            kind=kind,
            title=title[:255],
            content=content,
            content_hash=_content_hash(content),
            classification=classification.value,
            source_type=source_type,
            source_ref=source_ref,
            provenance=provenance or {},
            confidence=confidence,
            retention_until=retention_until,
            created_by=created_by,
        )
        self._session.add(item)
        await self._session.flush()

        chunks = _split_into_chunks(content)
        embedded = False
        for index, text in enumerate(chunks):
            vector: list[float] | None = None
            dimensions: int | None = None
            if self._embedder is not None:
                vector = await self._embedder.embed(text)
                dimensions = len(vector)
                if dimensions > MAX_EMBEDDING_DIMENSIONS:
                    msg = (
                        f"embedding has {dimensions} dimensions; pgvector HNSW supports at "
                        f"most {MAX_EMBEDDING_DIMENSIONS}"
                    )
                    raise ValidationError(msg, details={"dimensions": dimensions})
            self._session.add(
                MemoryChunk(
                    id=str(MemoryItemId.create()),
                    organization_id=self._org,
                    memory_item_id=item_id,
                    chunk_index=index,
                    content=text,
                    token_count=len(text) // 4,
                    embedding=vector,
                    embedding_model=self._embedding_model if vector is not None else None,
                    embedding_dimensions=dimensions,
                )
            )
            embedded = embedded or vector is not None

        await self._session.flush()
        return MemoryWriteResult(item_id=item_id, chunk_count=len(chunks), embedded=embedded)

    async def search(
        self,
        query: str,
        scope: MemoryScope,
        *,
        limit: int | None = None,
        min_score: float = 0.0,
    ) -> list[RetrievedMemory]:
        """Retrieve, filtered by scope before ranked.

        The filter is the security boundary; the ordering is a convenience. Every
        predicate below is part of what the requesting actor is allowed to see,
        not a post-processing step.
        """
        if not query.strip():
            msg = "search query must not be empty"
            raise ValidationError(msg, details={"field": "query"})

        limit = min(limit or scope.max_results, scope.max_results)
        if limit <= 0:
            return []

        stmt = (
            select(MemoryChunk, MemoryItem)
            .join(MemoryItem, MemoryItem.id == MemoryChunk.memory_item_id)
            .where(self._scope_predicates(scope))
        )
        stmt = stmt.order_by(MemoryChunk.chunk_index).limit(limit * 4)

        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return []

        query_vector = await self._embed_text(query) if self._embedder is not None else None
        results: list[RetrievedMemory] = []
        for chunk, item in rows:
            if query_vector is not None and chunk.embedding is not None:
                score = cosine_similarity(query_vector, list(chunk.embedding))
            else:
                # No embedder: fall back to a lexical overlap score so retrieval
                # is still useful and still deterministic. Silently returning
                # everything would be worse.
                score = _lexical_overlap(query, chunk.content)
            if score < min_score:
                continue
            results.append(
                RetrievedMemory(
                    item_id=item.id,
                    chunk_id=chunk.id,
                    content=chunk.content,
                    score=score,
                    tier=MemoryTier(item.tier),
                    classification=DataClassification(item.classification),
                    source_type=item.source_type,
                    source_ref=item.source_ref,
                    provenance=item.provenance or {},
                    confidence=item.confidence,
                    created_at=item.created_at,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    async def _embed_text(self, text: str) -> list[float]:
        vector: list[float] = await self._embedder.embed(text)
        return vector

    def _scope_predicates(self, scope: MemoryScope) -> Any:
        """Everything the requesting actor may see.

        Three narrowing rules, all required:
          1. the organization, always;
          2. a classification ceiling, always;
          3. ownership — an item scoped to an agent, a unit or a task is only
             visible when the request names that scope. An item with no owner is
             organisation-wide and visible to everyone in it.
        """
        predicates = [
            MemoryItem.organization_id == self._org,
            MemoryItem.is_deleted.is_(False),
        ]

        allowed_classifications = [
            c.value
            for c in DataClassification
            if not classification_at_least(c, scope.max_classification)
        ]
        predicates.append(MemoryItem.classification.in_(allowed_classifications))

        # Ownership. An org-wide item has all three owner columns NULL; an item
        # owned by anyone is visible only to a request naming that owner.
        predicates.append(
            (MemoryItem.agent_id.is_(None) | MemoryItem.agent_id.in_(list(scope.agent_ids)))
            if scope.agent_ids
            else MemoryItem.agent_id.is_(None)
        )
        predicates.append(
            (
                MemoryItem.org_unit_id.is_(None)
                | MemoryItem.org_unit_id.in_(list(scope.org_unit_ids))
            )
            if scope.org_unit_ids
            else MemoryItem.org_unit_id.is_(None)
        )
        predicates.append(
            (MemoryItem.task_id.is_(None) | MemoryItem.task_id.in_(list(scope.task_ids)))
            if scope.task_ids
            else MemoryItem.task_id.is_(None)
        )
        return and_(*predicates)

    async def get(self, item_id: str) -> MemoryItem:
        result = await self._session.execute(
            select(MemoryItem).where(
                and_(MemoryItem.id == item_id, MemoryItem.organization_id == self._org)
            )
        )
        item = result.scalar_one_or_none()
        if item is None:
            msg = f"memory item not found: {item_id}"
            raise NotFoundError(msg, resource_type="memory_item", resource_id=item_id)
        return item

    async def list_for_agent(self, agent_id: str, *, limit: int = 100) -> Sequence[MemoryItem]:
        result = await self._session.execute(
            select(MemoryItem)
            .where(
                and_(
                    MemoryItem.organization_id == self._org,
                    MemoryItem.agent_id == agent_id,
                    MemoryItem.is_deleted.is_(False),
                )
            )
            .order_by(MemoryItem.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def soft_delete(self, item_id: str) -> None:
        """Mark deleted and drop the chunks.

        A soft delete on the item and a hard delete on the chunks: the item row is
        the audit record, the vectors are the payload. A right-to-delete request
        that leaves the embeddings behind has not been honoured.
        """
        item = await self.get(item_id)
        item.is_deleted = True
        item.updated_at = utcnow()
        await self._session.execute(
            delete(MemoryChunk).where(MemoryChunk.memory_item_id == item_id)
        )
        await self._session.flush()

    async def purge_expired(self, *, now: Any = None) -> int:
        """Delete items whose retention has elapsed.

        Retention without enforcement is a promise nobody keeps, so this is
        callable and is called by the operations task.
        """
        moment = now or utcnow()
        ids = await self._session.execute(
            select(MemoryItem.id).where(
                and_(
                    MemoryItem.organization_id == self._org,
                    MemoryItem.retention_until.is_not(None),
                    MemoryItem.retention_until < moment,
                )
            )
        )
        expired = list(ids.scalars())
        if not expired:
            return 0
        await self._session.execute(
            delete(MemoryChunk).where(MemoryChunk.memory_item_id.in_(expired))
        )
        await self._session.execute(delete(MemoryItem).where(MemoryItem.id.in_(expired)))
        return len(expired)

    async def stats(self) -> dict[str, int]:
        result = await self._session.execute(
            select(MemoryItem.tier, func.count())
            .where(and_(MemoryItem.organization_id == self._org, MemoryItem.is_deleted.is_(False)))
            .group_by(MemoryItem.tier)
        )
        return {tier: count for tier, count in result.all()}


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _split_into_chunks(content: str) -> list[str]:
    """Split on paragraph boundaries, falling back to a sliding window.

    Paragraph-aware because a chunk that cuts a sentence in half retrieves
    badly and reads badly; the window is the fallback for content with no
    paragraph structure at all.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    if not paragraphs:
        paragraphs = [content]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= CHUNK_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= CHUNK_CHARS:
            current = paragraph
            continue
        # A single oversized paragraph: slide the window.
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start : start + CHUNK_CHARS])
            start += CHUNK_CHARS - CHUNK_OVERLAP
        current = ""
    if current:
        chunks.append(current)
    return chunks or [content[:CHUNK_CHARS]]


_TOKEN_RE = re.compile(r"[\w']+")


def _lexical_overlap(query: str, content: str) -> float:
    """Jaccard overlap of token sets. A fallback, not a semantic measure."""
    q = set(_TOKEN_RE.findall(query.lower()))
    c = set(_TOKEN_RE.findall(content.lower()))
    if not q or not c:
        return 0.0
    return len(q & c) / len(q | c)


class HashEmbedder:
    """Deterministic offline embedder. The default for tests and CI.

    Not semantic. Its value is that it is reproducible, free, and makes a test
    suite incapable of depending on a paid provider by accident.
    """

    name = "hash"

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        from ai_orchestrator.models.gateway import hash_embedding

        return hash_embedding(text, self.dimensions)


__all__ = [
    "CHUNK_CHARS",
    "MAX_EMBEDDING_DIMENSIONS",
    "HashEmbedder",
    "MemoryService",
    "MemoryWriteResult",
    "RetrievedMemory",
]
