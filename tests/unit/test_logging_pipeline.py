"""The logging pipeline must actually emit.

A renderer placed before `wrap_for_formatter` makes every record a rendered
string where the formatter expects a dict, and `logging` swallows the resulting
`AttributeError` and carries on. The process stays up, the tests pass, and the
log file contains nothing but `--- Logging error ---`. This file exists because
that happened, and because 656 other tests did not notice.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest
import structlog

from ai_orchestrator.config.settings import Settings
from ai_orchestrator.telemetry.logging import REDACTED, configure_logging, get_logger


@dataclass
class _Saved:
    """Process-wide logging state, so each test can hand it back unchanged."""

    handlers: list[logging.Handler] = field(default_factory=list)
    level: int = logging.NOTSET
    structlog_config: dict[str, object] = field(default_factory=dict)


@pytest.fixture
def saved() -> Iterator[_Saved]:
    """Logging is global and the suite shares a process; give it back intact."""
    root = logging.getLogger()
    state = _Saved(
        handlers=list(root.handlers),
        level=root.level,
        structlog_config=dict(structlog.get_config()),
    )
    try:
        yield state
    finally:
        root.handlers = state.handlers
        root.setLevel(state.level)
        structlog.configure(**state.structlog_config)  # type: ignore[arg-type]


def _emitted(settings: Settings, write: Callable[[], None]) -> str:
    """What the real formatter actually produced for one record."""
    configure_logging(settings)
    root = logging.getLogger()
    handler = root.handlers[0]
    buffer = io.StringIO()
    handler.setStream(buffer)
    try:
        write()
        handler.flush()
    finally:
        handler.setStream(sys.stdout)
    return buffer.getvalue()


#: A real key shape. The value filter is pattern matching, so a hand-written
#: near-miss like `sk-live-...` would be testing the pattern, not the pipeline.
_SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"


def _json_settings() -> Settings:
    return Settings(AO_ENVIRONMENT="test", log_json_text=True)


class TestStructlogRecords:
    def test_an_event_reaches_the_output(self, saved: _Saved) -> None:
        text = _emitted(_json_settings(), lambda: get_logger(__name__).info("hello.world", k="v"))
        assert "hello.world" in text, f"the event was swallowed; output was: {text!r}"
        assert '"k": "v"' in text or '"k":"v"' in text, text

    def test_an_event_is_redacted_on_the_way_out(self, saved: _Saved) -> None:
        """The value filter must still run now that the renderer moved."""
        text = _emitted(
            _json_settings(), lambda: get_logger(__name__).info("has.a.secret", token=_SECRET)
        )
        assert _SECRET not in text, text
        assert REDACTED in text, text

    def test_a_credential_in_a_free_text_value_is_caught(self, saved: _Saved) -> None:
        """Key-name matching is not the only defence, and cannot be the only one.

        A developer pastes a URL into a log field and the field name is `url`.
        """
        text = _emitted(
            _json_settings(),
            lambda: get_logger(__name__).info(
                "connect.failed",
                url="postgres://svc:hunter2hunter2@db.internal:5432/ao",
            ),
        )
        assert "hunter2hunter2" not in text, text


class TestForeignRecords:
    """A plain stdlib logger — `asyncio`, `httpx`, any dependency — is a different
    code path: it is re-rendered through `foreign_pre_chain` rather than arriving
    as an event dict."""

    def test_a_plain_record_reaches_the_output(self, saved: _Saved) -> None:
        text = _emitted(
            _json_settings(),
            lambda: logging.getLogger("some_dependency").warning("plain text record"),
        )
        assert "plain text record" in text, f"a foreign record was swallowed; output was: {text!r}"

    def test_a_foreign_record_is_redacted(self, saved: _Saved) -> None:
        """A credential in a dependency's own log line is still a leak in ours.

        `foreign_pre_chain` is the only thing standing between an `httpx` debug
        line and a log index, so redaction has to run on this path too. The
        values are real key shapes, because the value filter is pattern matching
        and a hand-written near-miss would test the pattern, not the pipeline.
        """
        for secret in (
            _SECRET,
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "postgres://svc:hunter2hunter2@db.internal:5432/ao",
        ):
            text = _emitted(
                _json_settings(),
                lambda s=secret: logging.getLogger("some_dependency").warning(f"auth: {s}"),
            )
            assert secret not in text, f"a dependency log line leaked {secret!r}: {text}"


class TestNoSilentLoss:
    def test_a_broken_formatter_is_not_merely_printed_to_stderr(self, saved: _Saved) -> None:
        """The original defect, asserted as an absence.

        `logging` reports a broken formatter by printing to stderr and dropping
        the record, so the missing output is indistinguishable from "nothing was
        logged". Asserting on stderr is the only way to see the difference.
        """
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            text = _emitted(
                _json_settings(), lambda: get_logger(__name__).info("a.line.that.must.land")
            )
        assert "Logging error" not in stderr.getvalue(), stderr.getvalue()
        assert "a.line.that.must.land" in text, text
