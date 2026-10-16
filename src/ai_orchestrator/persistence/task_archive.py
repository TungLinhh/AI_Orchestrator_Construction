"""Reversible task visibility metadata; task state and evidence remain intact."""

from sqlalchemy import and_, not_
from sqlalchemy.sql.elements import ColumnElement

from ai_orchestrator.persistence.models import Task

ARCHIVE_KEY = "_synthetic_archive"
ARCHIVE_SIGNATURE = {ARCHIVE_KEY: {"version": 1, "state": "archived"}}
TERMINAL = ("completed", "failed", "canceled", "expired")


def visible_tasks() -> ColumnElement[bool]:
    """Malformed metadata and live tasks always remain visible."""
    return not_(and_(Task.status.in_(TERMINAL), Task.constraints.contains(ARCHIVE_SIGNATURE)))


# Constant expression for the read models using SQL rather than ORM queries.
VISIBLE_TASK_SQL = """NOT (t.status IN ('completed','failed','canceled','expired')
    AND t.constraints @> jsonb_build_object('_synthetic_archive',
        jsonb_build_object('version', 1, 'state', 'archived')))"""
