"""Procedure fingerprints: what shape of work this was, independent of its wording.

`task_fingerprint` answers *"have I been asked this before?"* — two tasks whose
goals differ by a character are different tasks, which is exactly what
deduplication needs and exactly what repetition detection must not use.

A procedure answers a different question: *"is this the same shape of work?"* The
same purchase requisition arrives as "buy 5 laptops" and "buy 6 laptops and a
printer" and "Tôi cần mua 5 chiếc laptop cho Marketing", and all three are the same
procedure. The first live `headcount-request` run made this concrete: it produced
three child tasks whose goals differed by a prefix, so their task fingerprints
differed, while their tool sequences were the same work done three times.

Four deliberate choices, each of which can produce a wrong answer, so each is
recorded here rather than left to be rediscovered:

**Argument keys, never argument values.** A fingerprint over values treats "buy 5
laptops" and "buy 6 laptops" as different procedures, which is the failure mode of
having no procedure at all wearing a hash. The *names* of the arguments are part of
the shape; what was passed is the content, and content is what a lesson is about.

**The tool sequence, ordered.** A procedure is a sequence of steps. Sorting the
names would make `write_report` then `delegate_to_agent` the same procedure as the
reverse, and the two are not the same work.

**Consecutive repeats collapse, and this is the one that was got wrong first.** The
fingerprint originally hashed the whole step list, repetitions included. On live
traffic that meant four runs of the same quarterly report produced four different
fingerprints, because the model searched a different number of times each run — and
since the repetition gate counts identical fingerprints, it never opened. A count of
repetitions is the one property of a run guaranteed to vary, so hashing it
guarantees the gate stays shut. See `Procedure.shape`.

**A refusal is part of the shape, and is distinguishable from a call.** An agent
that asked for a tool and was refused, and one that got what it asked for, have
performed the same procedure as far as the fingerprint is concerned — but not as far
as a lesson is concerned. So the outcome is folded in as a single character, and
the difference between "refused 3 times" and "refused once" stays visible in the
trace rather than collapsing into the same key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

#: Version tag in the hashed payload. If the shape of the fingerprint ever changes,
#: every stored value is stale and the tag makes that visible instead of silently
#: comparing fingerprints that were computed under different rules.
#: Version tag in the hashed payload. If the shape of the fingerprint ever changes,
#: every stored value is stale and the tag makes that visible instead of silently
#: comparing fingerprints that were computed under different rules.
#:
#: `v2` collapses consecutive repeats. `v1` hashed the full step list, so a run that
#: searched 18 times and one that searched 21 were different procedures — which meant
#: the repetition gate could never open on real traffic, only on a scripted runtime
#: that happened to be perfectly repeatable. Every fingerprint stored under `v1` is
#: now an orphan by design, and the tag is what makes that visible rather than
#: mysterious.
FINGERPRINT_VERSION = "proc-v2"


@dataclass(frozen=True, slots=True)
class ToolStep:
    """One tool call, reduced to the parts that define the shape."""

    name: str
    #: Sorted argument *keys*. Values are deliberately absent — see the module
    #: docstring.
    argument_keys: tuple[str, ...] = ()
    #: Whether the call was refused. Distinguishes "tried and was told no" from
    #: "did it", which a lesson needs and a fingerprint should not hide.
    refused: bool = False

    def as_text(self) -> str:
        keys = ",".join(self.argument_keys)
        return f"{self.name}({keys}){'-refused' if self.refused else ''}"


@dataclass(frozen=True, slots=True)
class Procedure:
    """A task type plus the sequence of tool calls that carried it out."""

    task_type: str
    steps: tuple[ToolStep, ...] = field(default_factory=tuple)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps)

    def shape(self) -> tuple[ToolStep, ...]:
        """The procedure with consecutive repeats collapsed.

        **This is the fingerprint, and getting it wrong makes the whole learning
        loop dead.** Measured on live traffic: four runs of the same quarterly
        report produced four different fingerprints, because one searched 18 times
        before writing and the next searched 21. They are the same procedure — look
        something up, then write it up — and the repetition gate counts *identical*
        fingerprints, so it sat at 1 while the platform did the same thing four times
        in a row. A count of repetitions is the one thing about a run that always
        varies, so including it guarantees the gate never opens.

        Only *consecutive* repeats collapse. `search, write, search` and
        `search, search, write` stay different, because going back to look something
        up after writing is a different shape of work from searching twice up front,
        and a lesson about one is not a lesson about the other.

        The counts are not discarded — they are in the trace, in the evidence packet,
        and in the model's own output. They are simply not part of the shape.
        """
        collapsed: list[ToolStep] = []
        for step in self.steps:
            if collapsed and collapsed[-1] == step:
                continue
            collapsed.append(step)
        return tuple(collapsed)

    def as_text(self) -> str:
        lines = [f"{FINGERPRINT_VERSION}\t{self.task_type}"]
        lines.extend(step.as_text() for step in self.shape())
        return "\n".join(lines)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.as_text().encode("utf-8")).hexdigest()


#: The audit outcomes that mean "this call did not take effect".
#:
#: Read from the writers, not from memory. `task_execution` writes `failure` for a
#: tool that errored and `blocked` for one a policy stopped, and the column default is
#: `success`. Checking only for `failure` made every policy-blocked call fingerprint
#: identically to a successful one, so a run in which the platform refused a write was
#: indistinguishable from a run where the write happened — and the repetition gate
#: counted them as the same procedure.
_NOT_EFFECTIVE = frozenset({"failure", "blocked"})


def procedure_fingerprint(
    *,
    task_type: str,
    tool_calls: Sequence[dict[str, object]],
) -> str:
    """The fingerprint of a run, from its raw `tool.invoke` audit rows.

    Takes the rows rather than a `Procedure` so callers cannot accidentally build
    the steps wrongly: the only way in is the trace, which is the record of what
    actually happened rather than a reconstruction of it.

    Rows are sorted by `sequence`, so the order is the order the calls were made
    and not the order a query happened to return them.
    """
    ordered = sorted(tool_calls, key=_sequence_of)
    steps = tuple(
        ToolStep(
            name=str(row.get("tool") or ""),
            argument_keys=tuple(sorted(_argument_keys(row.get("arguments")))),
            refused=str(row.get("outcome") or "") in _NOT_EFFECTIVE,
        )
        for row in ordered
        if row.get("tool")
    )
    return Procedure(task_type=task_type, steps=steps).fingerprint()


def _sequence_of(row: dict[str, object]) -> int:
    """The audit sequence, defensively.

    The trace column is a bigint and always present, so this is defensive rather
    than necessary — but a `None` here would raise inside a sort key, which is a
    miserable place to discover a nullable column.
    """
    value = row.get("sequence")
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return 0


def _argument_keys(arguments: object) -> Sequence[str]:
    if not isinstance(arguments, dict):
        return ()
    return [str(key) for key in arguments]


def describe(procedure_fingerprint_hex: str) -> str:
    """A short, stable label for a fingerprint, for logs and approval packets.

    The first eight hex characters. Not reversible and not meant to be — a label
    that claims to describe a procedure but cannot is worse than one that admits
    it is a label.
    """
    return procedure_fingerprint_hex[:8]


def same_procedure(left: str, right: str) -> bool:
    """Whether two fingerprints are the same procedure.

    A named function rather than `==` at the call sites, so that the version tag
    can be honoured here if the shape ever changes. Two fingerprints computed under
    different versions are *not* the same procedure even when the hex matches, and
    the day that stops being true is the day this function earns its existence.
    """
    return bool(left) and left == right


__all__ = [
    "FINGERPRINT_VERSION",
    "Procedure",
    "ToolStep",
    "describe",
    "procedure_fingerprint",
    "same_procedure",
]
