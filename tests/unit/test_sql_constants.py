"""Every SQL constant in the package, checked as SQL rather than as prose.

Two bugs, both in one query, both found by *calling* the endpoint and neither found by
reading it. They are here because both are invisible to a type checker, a linter and a
careful reader, and both produce a 500 for the simplest possible request.

**A `--` comment inside a `text()` constant is a liability.** `readings_for_node` has
one, and while adding the same lesson to `agents_control` I wrote this inside the string:

    -- A bare `:since IS NULL` is a separate bind from the `:since` inside the `CAST`

SQLAlchemy's `text()` scans the **whole string** for `:name`, comments included, and
rewrote both occurrences to `$2` — inside a comment. The statement still parsed, and the
explanation of the bug was silently becoming part of the bug's own SQL.

**`#` is not a SQL comment.** A `# omits all three` line, left behind by an edit that
mangled a `--` into a `#`, was sent to Postgres as SQL. `syntax error at or near "all"`,
from a line in a comment that was not one.

Neither is exotic. Both are the same lesson the whole repository keeps learning: **prose
belongs in Python, above the constant, not inside the statement that is sent over the
wire.** So this file makes it a rule rather than a habit.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

#: `src/ai_orchestrator` — the application, domain and API layers. `migrations/` is
#: deliberately excluded: an Alembic file is a Python script that *emits* SQL, and its
#: `op.execute("""...""")` blocks are as subject to these rules as anything here.
SOURCE = pathlib.Path("src/ai_orchestrator")

#: A Python `#` or a SQL `--` that begins a line inside a SQL string.
_HASH_COMMENT = re.compile(r"^\s*#(?!:)")
_SQL_COMMENT = re.compile(r"^\s*--(?!\s*$)")

#: Names that are *supposed* to look like binds when they appear in prose, and are
#: therefore not evidence of anything.
_ALLOWED = frozenset({"__file__", "__name__", "__doc__"})


def _sql_constants() -> list[tuple[pathlib.Path, int, str, str]]:
    """Every `NAME = "..."` that is passed to `text()`, with its source."""
    found: list[tuple[pathlib.Path, int, str, str]] = []
    for path in sorted(SOURCE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node.value.value, str):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "text" for t in ast.walk(node)
            ) and not _looks_like_sql(node.value.value):
                continue
            name = node.targets[0].id if isinstance(node.targets[0], ast.Name) else "?"
            found.append((path, node.lineno, name, node.value.value))
    return found


def _looks_like_sql(value: str) -> bool:
    head = value.lstrip().upper()
    return head.startswith(("SELECT", "INSERT", "UPDATE", "DELETE", "WITH"))


CONSTANTS = _sql_constants()


def test_the_suite_actually_found_the_sql() -> None:
    """A guard that guards nothing is worse than no guard.

    If the AST walk ever stops matching, every test below passes vacuously and reports
    a clean bill of health for a set of files it never read. So the count is asserted,
    and it is asserted to be *large* — a walk that finds three constants when there are
    thirty is broken, not strict.
    """
    assert len(CONSTANTS) >= 40, (
        f"only {len(CONSTANTS)} SQL constants found; the AST walk has probably broken "
        "and every check below is now vacuous"
    )


@pytest.mark.parametrize(
    ("path", "lineno", "name", "sql"),
    CONSTANTS,
    ids=[f"{p.name}:{n}" for p, n, _, _ in CONSTANTS],
)
class TestEverySqlConstant:
    def test_it_has_no_python_hash_comment(
        self, path: pathlib.Path, lineno: int, name: str, sql: str
    ) -> None:
        """`#` is not SQL. A line starting with one is sent to Postgres as SQL.

        F128: one such line, left by an edit that turned a `--` into a `#`, produced
        `syntax error at or near "all"` on every request to the endpoint.
        """
        offenders = [
            f"{path.name}:{lineno + i + 1}  {line.strip()}"
            for i, line in enumerate(sql.splitlines())
            if _HASH_COMMENT.match(line)
        ]
        assert not offenders, (
            f"{name} contains a `#` line, which SQL will not treat as a comment:\n  "
            + "\n  ".join(offenders)
        )

    def test_no_comment_mentions_a_bind(
        self, path: pathlib.Path, lineno: int, name: str, sql: str
    ) -> None:
        """A `:name` inside a `--` comment is rewritten to a positional bind.

        F128: SQLAlchemy scans the whole `text()` string, so the comment explaining
        `:since IS NULL` had its own `:since` rewritten to `$2` — the explanation was
        sent to Postgres as part of the statement. The comment is still a comment so it
        parses, which is why it survived as long as it did; it is simply a statement
        carrying an explanation nobody wrote for the server.
        """
        offenders = []
        for i, line in enumerate(sql.splitlines()):
            stripped = line.strip()
            if not stripped.startswith("--"):
                continue
            body = stripped[2:]
            # `:name` where name is a real identifier. Backticked prose and file paths
            # are excluded because a reader writes `` `:since` `` while explaining.
            for m in re.finditer(r"(?<!`):([A-Za-z_][A-Za-z0-9_]*)", body):
                if m.group(1) not in _ALLOWED and "`" not in body:
                    offenders.append(f"{path.name}:{lineno + i + 1}  {stripped[:70]}")
                    break
        assert not offenders, (
            f"{name} has a comment naming a bind parameter; move the prose above the "
            "constant:\n  " + "\n  ".join(offenders)
        )

    def test_it_ends_with_a_semicolon_or_a_complete_statement(
        self, path: pathlib.Path, lineno: int, name: str, sql: str
    ) -> None:
        """Balanced parentheses, and no unterminated string or comment.

        Cheap, and it is the check that would have caught the `#` line before it
        reached a database.
        """
        code = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
        assert code.count("(") == code.count(")"), (
            f"{name} has unbalanced parentheses: {code.count('(')} open, {code.count(')')} close"
        )
        assert code.count("'") % 2 == 0, f"{name} has an odd number of single quotes"
        assert "``" not in code, f"{name} carries markdown backticks into SQL"
