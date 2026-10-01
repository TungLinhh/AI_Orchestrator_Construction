"""ASGI entry point. `uvicorn ai_orchestrator.main:app` runs this."""

from __future__ import annotations

from ai_orchestrator.api.app import app

__all__ = ["app"]
