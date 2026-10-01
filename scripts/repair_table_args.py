"""Remove a `__table_args__ = domain_args(` that is immediately followed by class columns.

That shape is always a mistake: a `domain_args(` call has to be followed by arguments.
An opening followed by a blank line and an attribute declaration is a leftover whose
arguments were lost, and leaving it in place makes the class declare **no** constraints
while reading as though it declares some.

Detected by what follows rather than by a marker, so it cannot miss a variant. Reported
per class, because a silent removal is how six indexes went missing in the first place.
"""

from __future__ import annotations

import pathlib
import re
import sys

MODULES = ("procurement", "supply", "contracts", "commercial", "models")
BASE = pathlib.Path("src/ai_orchestrator/persistence")
OPENING = re.compile(r"^(\s*)__table_args__ = domain_args\($")
ATTRIBUTE = re.compile(r"^\s*[a-z_][a-z0-9_]*: M\w")


def repair(path: pathlib.Path) -> list[str]:
    lines = path.read_text().split("\n")
    out: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(lines):
        m = OPENING.match(lines[i])
        if m is None:
            out.append(lines[i])
            i += 1
            continue
        # Look ahead past blank lines and comment lines for the next real line.
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
            j += 1
        if j < len(lines) and ATTRIBUTE.match(lines[j]):
            table = None
            for back in range(len(out) - 1, -1, -1):
                mm = re.match(r'^\s*__tablename__ = "(\w+)"$', out[back])
                if mm:
                    table = mm.group(1)
                    break
            removed.append(table or "<unknown>")
            i = j
            continue
        out.append(lines[i])
        i += 1
    if removed:
        path.write_text("\n".join(out))
    return removed


def main() -> int:
    for name in MODULES:
        path = BASE / f"{name}.py"
        if not path.exists():
            continue
        gone = repair(path)
        if gone:
            print(f"  {name}.py: removed a broken `__table_args__` opening from {gone}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
