"""Immutable source intake and export records. Constraints are schema-generated."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import JSONB, Base, created_at_col


class SourceFile(Base):
    __tablename__ = "source_files"

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "org_unit_id"],
            ["organizational_units.organization_id", "organizational_units.id"],
            name="fk_source_files_organization_id_organizational_units",
        ),
        Index(
            "ix_source_files_organization_id_created_at",
            "organization_id",
            "created_at",
        ),
        UniqueConstraint("organization_id", "id", name="uq_source_files_org_id"),
        CheckConstraint(
            "(((byte_count > 0) AND (byte_count <= 10485760)))",
            name="size_bounded",
        ),
        CheckConstraint(
            "(((state)::text = ANY ((ARRAY['ready'::character varying, "
            "'needs_review'::character varying, 'unsupported'::character varying, "
            "'failed'::character varying])::text[])))",
            name="state_known",
        ),
        CheckConstraint(
            "(((origin)::text = ANY ((ARRAY['upload'::character varying, "
            "'mock'::character varying])::text[])))",
            name="origin_known",
        ),
        CheckConstraint(
            "(((classification)::text = ANY ((ARRAY['public'::character varying, "
            "'internal'::character varying, 'confidential'::character varying, "
            "'restricted'::character varying, 'secret'::character varying])::text[])))",
            name="classification_known",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("organizations.id"), nullable=False
    )
    org_unit_id: Mapped[str | None] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    classification: Mapped[str] = mapped_column(String(20), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    extraction: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at_col()


class SourceManifest(Base):
    __tablename__ = "source_manifests"

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_source_manifests_organization_id_tasks",
        ),
        UniqueConstraint("organization_id", "id", name="uq_source_manifests_org_id"),
        UniqueConstraint("organization_id", "task_id", name="uq_source_manifests_org_task"),
        CheckConstraint(
            "(((selection)::text = ANY ((ARRAY['sources'::character varying, "
            "'mock'::character varying])::text[])))",
            name="selection_known",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("organizations.id"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(40), nullable=False)
    selection: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = created_at_col()


class SourceBinding(Base):
    __tablename__ = "source_bindings"

    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "manifest_id", "file_id"),
        ForeignKeyConstraint(
            ["organization_id", "file_id"],
            ["source_files.organization_id", "source_files.id"],
            name="fk_source_bindings_organization_id_source_files",
        ),
        ForeignKeyConstraint(
            ["organization_id", "manifest_id"],
            ["source_manifests.organization_id", "source_manifests.id"],
            name="fk_source_bindings_organization_id_source_manifests",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("organizations.id"), primary_key=True
    )
    manifest_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(40), primary_key=True)


class SourceArtifact(Base):
    __tablename__ = "source_artifacts"

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "manifest_id"],
            ["source_manifests.organization_id", "source_manifests.id"],
            name="fk_source_artifacts_organization_id_source_manifests",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_source_artifacts_organization_id_tasks",
        ),
        UniqueConstraint(
            "organization_id",
            "task_id",
            "format",
            name="uq_source_artifacts_org_task_format",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("organizations.id"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest_id: Mapped[str] = mapped_column(String(40), nullable=False)
    format: Mapped[str] = mapped_column(String(12), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[dt.datetime] = created_at_col()
