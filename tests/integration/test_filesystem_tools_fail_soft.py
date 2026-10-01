"""A filesystem problem is a tool result, not a dead agent run.

Two live child agents died with `Tool 'write_report' exceeded max retries count
of 0`. Every tool is registered with `max_retries=0`, which is correct — the
gateway owns retry policy, and a framework retry would re-run a side-effecting tool
with no regard for whether it already ran. But it also means a raised error is
fatal, and `OSError` from the filesystem was raising.

A full disk, a read-only mount, a directory where a file was expected: these are
ordinary conditions for a long-lived agent. They must arrive at the model as a
refusal it can read and reason about, because the alternative is an agent that
produces nothing at all and a task marked `internal_error`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_orchestrator.domain.policy import RuleBasedPolicyEngine, default_policy_set
from ai_orchestrator.tools.builtin import _read_document, _write_artifact, build_default_tools
from ai_orchestrator.tools.gateway import ToolGateway
from ai_orchestrator.tools.registry import ToolResult

pytestmark = pytest.mark.integration

ORG = "org_01m3d5hwxet3x61vjc1ffjyrzh"
AGENT = "agt_01m3d5hwxet3x61vjc1ffjyrzj"


def _agent(org: str):  # type: ignore[no-untyped-def]
    from ai_orchestrator.domain.contracts import Actor
    from ai_orchestrator.domain.enums import ActorType

    return Actor(id=AGENT, kind=ActorType.AGENT, organization_id=org)  # type: ignore[arg-type]


def _gateway() -> ToolGateway:
    return ToolGateway(
        build_default_tools(), policy_engine=RuleBasedPolicyEngine(default_policy_set().rules)
    )


class TestTheHelpersDoNotRaise:
    def test_an_unwritable_root_returns_empty_rather_than_raising(self, tmp_path: Path) -> None:
        """A read-only directory is the realistic case, and it must not propagate."""
        root = tmp_path / "readonly"
        root.mkdir()
        root.chmod(0o500)
        try:
            assert _write_artifact(root, ORG, "report.md", "body") == ""
        finally:
            root.chmod(0o700)

    def test_a_directory_where_a_file_should_be_is_missing_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        """A directory is not a document, and saying so is a correct answer.

        `is_file()` is false for a directory, so this returns `missing` before any
        read is attempted. Asserted because the two are genuinely different: an
        unreadable *file* is a filesystem problem, and a directory at that path is
        a wrong request. Neither may raise.
        """
        (tmp_path / ORG / "a-directory").mkdir(parents=True)
        assert _read_document(tmp_path, ORG, "a-directory") == ("missing", "")

    def test_an_unreadable_file_is_unreadable_not_a_crash(self, tmp_path: Path) -> None:
        """The case `is_file()` passes and the read still fails on."""
        target = tmp_path / ORG / "secret.md"
        target.parent.mkdir(parents=True)
        target.write_text("x")
        target.chmod(0o000)
        try:
            # Running as root defeats the permission bits, which is a fact about
            # the machine and not about the code. Skipped rather than asserted
            # green, because a test that cannot fail is worse than no test.
            if os.geteuid() == 0:
                pytest.skip("running as root: permission bits do not deny access")
            assert _read_document(tmp_path, ORG, "secret.md") == ("unreadable", "")
        finally:
            target.chmod(0o600)

    def test_a_missing_document_is_still_missing(self, tmp_path: Path) -> None:
        (tmp_path / ORG).mkdir(parents=True)
        assert _read_document(tmp_path, ORG, "absent.md") == ("missing", "")

    def test_a_working_write_still_works(self, tmp_path: Path) -> None:
        written = _write_artifact(tmp_path, ORG, "report.md", "the body")
        assert written, "a writable root stopped working"
        assert Path(written).read_text() == "the body"


class TestTheToolReportsRefusals:
    async def test_an_unwritable_artifact_is_a_refusal_not_an_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end property: the model gets a result it can read."""
        monkeypatch.setenv("AO_ARTIFACT_ROOT", str(tmp_path / "ro"))
        (tmp_path / "ro").mkdir()
        (tmp_path / "ro").chmod(0o500)
        try:
            invocation = await _gateway().invoke(
                actor=_agent(ORG),
                tool_name="write_report",
                arguments={"title": "Job Description", "body": "A senior role."},
                organization_id=ORG,
            )
        finally:
            (tmp_path / "ro").chmod(0o700)

        assert invocation.result is not None, "the gateway returned no result at all"
        assert isinstance(invocation.result, ToolResult)
        assert not invocation.result.ok, "an unwritable artifact reported success"
        assert invocation.result.error_kind == "ARTIFACT_NOT_WRITTEN"

    async def test_the_refusal_explains_itself(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("AO_ARTIFACT_ROOT", str(tmp_path / "ro2"))
        (tmp_path / "ro2").mkdir()
        (tmp_path / "ro2").chmod(0o500)
        try:
            invocation = await _gateway().invoke(
                actor=_agent(ORG),
                tool_name="write_report",
                arguments={"title": "t", "body": "b"},
                organization_id=ORG,
            )
        finally:
            (tmp_path / "ro2").chmod(0o700)
        assert invocation.result is not None
        message = invocation.result.error_message or ""
        # Both causes, because the model cannot tell them apart and neither is
        # actionable without knowing which. "PATH_ESCAPE" alone would have sent an
        # operator looking for a containment bug that did not happen.
        assert "escaped" in message and "filesystem" in message, message
