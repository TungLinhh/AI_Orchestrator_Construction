"""Structured logging.

JSON to stdout, one object per line, because the consumer is a machine. The
renderers differ only in destination: a console renderer for `make dev`, a JSON
renderer everywhere else.

The important part is what never gets logged. `redact()` is applied to every
log's `event_dict`, at the processor, so a developer adding a field does not
have to remember. That is the same reasoning as the telemetry scrubber: the
control has to be at the boundary, because the mistake is easy and the
consequence is a credential in a log index.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from ai_orchestrator.config.settings import Settings

#: Key names that must never be emitted. Matched case-insensitively.
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"token|authorization|auth[_-]?header|credential|cookie|"
    r"set-cookie|session[_-]?id|dsn|connection[_-]?string)"
)

#: Values that look like credentials regardless of the key they are under.
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(sk-or-v1-[A-Za-z0-9]+|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"gho_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]+|Bearer\s+[A-Za-z0-9._-]{16,}|"
    r"postgres(?:ql)?://[^\s:]+:[^\s@]+@)"
)

REDACTED = "[redacted]"


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively replace anything that looks like a credential.

    Depth-limited so a self-referential structure cannot spin, and so a large
    payload cannot turn logging into a denial of service.
    """
    if _depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _SECRET_KEY_PATTERN.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact(val, _depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str) and _SECRET_VALUE_PATTERN.search(value):
        return REDACTED
    return value


def _redact_processor(logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor. Applied to every event on its way out."""
    redacted: dict[str, Any] = redact(event_dict)
    return redacted


def configure_logging(settings: Settings) -> None:
    """Install structlog as the logging backend for the whole process."""
    # Processors only. No renderer: the renderer belongs to the
    # `ProcessorFormatter` below, which runs it once, at the end, for both
    # structlog and foreign records. Putting a renderer *before*
    # `wrap_for_formatter` makes the formatter receive a rendered string where
    # it expects an event dict, and every log line in the process then dies with
    # `AttributeError: 'str' object has no attribute 'copy'` — silently, because
    # logging swallows formatter errors and carries on.
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        # Timestamps in UTC with a T separator: log aggregation sorts these
        # lexicographically, and a local-time log is unreadable during an
        # incident that spans two regions.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
        # Force a useful `event` even when a caller passes a message positionally.
        structlog.processors.EventRenamer("event"),
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    render: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if settings.log_json_text
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        # Foreign records are plain stdlib ones — `asyncio`, `httpx`, an
        # unconfigured dependency. They arrive as a string and are re-rendered
        # through the same processors so they are not the one unredacted line in
        # the file.
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            render,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # These libraries are extremely chatty at INFO and drown the application
    # log. Raising them to WARNING is what makes the application log readable.
    for noisy in ("sqlalchemy.engine", "aiosqlite", "asyncio", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_context(**values: Any) -> None:
    """Attach values to every subsequent log in this task or request.

    structlog's contextvars integration means an organisation id bound once at
    the request boundary appears on every line without being passed down.
    """
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


__all__ = [
    "REDACTED",
    "bind_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "redact",
]
