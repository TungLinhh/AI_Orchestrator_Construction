"""Domain layer purity.

The domain is where the business rules live. If it can reach a database, a
socket or the clock, then testing those rules requires a running system, and the
rules that matter most — the ones that stop an agent from doing something
harmful — become the hardest to test and the least tested.

This test is cheap and catches a regression that would be very expensive to
find later.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "src" / "ai_orchestrator" / "domain"

#: Modules that imply I/O. `time` is allowed only inside `ids.py`, which needs
#: the wall clock for ULID monotonicity and is asserted separately.
FORBIDDEN_TOP_LEVEL = {
    "asyncio",
    "httpx",
    "requests",
    "socket",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "redis",
    "nats",
    "temporalio",
    "fastapi",
    "pydantic_ai",
    "structlog",
    "boto3",
    "urllib",
    "urllib3",
}

#: Domain must not import any sibling layer of the package.
FORBIDDEN_PACKAGE_PREFIXES = (
    "ai_orchestrator.infrastructure",
    "ai_orchestrator.application",
    "ai_orchestrator.api",
    "ai_orchestrator.agent_runtime",
    "ai_orchestrator.policies",
    "ai_orchestrator.persistence",
)


def _module_paths() -> list[Path]:
    return sorted(DOMAIN_DIR.glob("*.py"))


def test_domain_directory_is_populated() -> None:
    assert len(_module_paths()) >= 8, "the domain layer should hold the core rules"


@pytest.mark.parametrize("path", _module_paths(), ids=lambda p: p.name)
def test_domain_module_has_no_io_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in FORBIDDEN_TOP_LEVEL, (
                    f"{path.name} imports {alias.name}; the domain layer must stay pure"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in FORBIDDEN_TOP_LEVEL, (
                f"{path.name} imports from {node.module}; the domain layer must stay pure"
            )


@pytest.mark.parametrize("path", _module_paths(), ids=lambda p: p.name)
def test_domain_module_does_not_import_other_layers(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_PACKAGE_PREFIXES), (
                    f"{path.name} imports {alias.name}; dependencies must point inwards"
                )
        if module:
            assert not module.startswith(FORBIDDEN_PACKAGE_PREFIXES), (
                f"{path.name} imports {module}; dependencies must point inwards"
            )


def test_only_ids_module_reads_the_wall_clock() -> None:
    """Only ULID minting may read the clock.

    A default like `datetime.now()` on a domain model makes the model's value
    depend on when it was constructed, which makes time-dependent behaviour
    (TTL, expiry, staleness) untestable without freezing time. Timestamps that
    are part of a value object are therefore required, not defaulted.
    """
    offenders: list[str] = []
    for path in _module_paths():
        if path.name == "ids.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # `datetime.now(...)`, `date.today()`, `time.time()`
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"now", "utcnow", "today", "time"}
            ):
                base = node.func.value
                name = getattr(base, "id", getattr(base, "attr", ""))
                if name in {"datetime", "date", "time", "dt", "_utcnow"}:
                    offenders.append(f"{path.name}:{node.lineno} {name}.{node.func.attr}()")
            # `default_factory=<clock callable>`. Deterministic factories
            # (dict, list, frozenset) are fine; only time-dependent ones are not.
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg != "default_factory":
                        continue
                    target = kw.value
                    rendered = ast.unparse(target) if target is not None else "?"
                    if any(
                        token in rendered
                        for token in ("now", "utcnow", "today", "time", "_now", "clock")
                    ):
                        offenders.append(f"{path.name}:{node.lineno} default_factory={rendered}")
    assert not offenders, (
        "domain models must require their timestamps instead of defaulting to now: "
        + ", ".join(offenders)
    )


def test_ulid_is_monotonic_and_prefixed() -> None:
    from ai_orchestrator.domain.ids import is_valid_ulid, make_id

    first, second = make_id("agt"), make_id("agt")
    assert first < second, "ids must sort by creation order for keyset pagination"
    assert is_valid_ulid(first.removeprefix("agt_"))
    with pytest.raises(ValueError, match="invalid id prefix"):
        make_id("AGENT_X")


def test_branded_ids_are_not_interchangeable() -> None:
    """The type identity is the guard against cross-entity id confusion."""
    from ai_orchestrator.domain.ids import AgentId, TaskId

    assert not isinstance(AgentId.create(), TaskId)
    assert AgentId.create().startswith("agt_")
    assert TaskId.create().startswith("tsk_")
