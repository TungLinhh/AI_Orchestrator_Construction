"""Delete the earlier copies of a duplicated class attribute, keeping the last.

`python scripts/dedupe_class_body.py <file> [<file> ...]`

SQLAlchemy reads a class body top to bottom, so a repeated `AnnAssign` silently wins —
which is why a duplicated body imports cleanly, declares the right columns, and is
invisible until mypy reports `Name "id" already defined`.

**The last copy is kept, deliberately.** The first script here kept the *first* and lost
a column, because the two copies were not identical: the second had a column the first
did not. Keeping the last is the safe direction for a body that has been appended to, and
it is also the direction SQLAlchemy itself resolves in — so the deduplicated class
declares exactly what the duplicated one did.

Every class is reported with how many statements went, and the caller is expected to
check the resulting column count against the database rather than trust the number.
"""

from __future__ import annotations

import ast
import pathlib
import sys


def dedupe(path: pathlib.Path) -> list[tuple[str, int, int]]:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    lines = source.split("\n")
    kill: list[tuple[int, int]] = []
    report: list[tuple[str, int, int]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        occurrences: dict[str, list[tuple[int, int]]] = {}
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            occurrences.setdefault(stmt.target.id, []).append(
                (stmt.lineno, stmt.end_lineno or stmt.lineno)
            )
        removed = kept = 0
        for spans in occurrences.values():
            if len(spans) < 2:
                continue
            # Keep the last; delete the rest, whole statements.
            for start, end in spans[:-1]:
                kill.append((start, end))
                removed += 1
            kept += 1
        if removed:
            report.append((node.name, removed, kept))

    for start, end in sorted(kill, reverse=True):
        del lines[start - 1 : end]
        # Swallow a single blank line left behind, so the class body stays readable.
        if start - 1 < len(lines) and not lines[start - 1].strip() and not lines[start].strip():
            del lines[start - 1]
    if kill:
        path.write_text("\n".join(lines))
    return report


def main(argv: list[str] | None = None) -> int:
    for name in argv or sys.argv[1:]:
        path = pathlib.Path(name)
        if not path.exists():
            continue
        for cls, removed, kept in dedupe(path):
            print(
                f"  {path.name}: {cls}: removed {removed} earlier copy/copies, "
                f"kept the last of {kept} attribute(s)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
