"""What a task said it would produce, and whether it did.

## The gap this closes, measured

`tasks.expected_output_schema` was **stored, passed to the agent in its context, and never
checked**. Measured on the development tenant: **113 tasks** carried one, and nothing anywhere
read it back. So a task could declare `produces: approved_headcount`, finish `completed`, and
write `{proposal_count, scripted}` instead — which is exactly what happened when the first
stage of `ONX-BO-HR-SOP-004` was run for real. The stage reported success and produced
something else.

A declared output that is never checked is a **comment in a column**. The process advanced, the
UI showed the stage green, and nobody was told.

## Two shapes, one question

The column is read as either:

```python
{"required": ["job_description", "rubric"]}   # explicit list
{"produces": "job_description, rubric"}       # the label the 113 existing rows use
```

Both ask *"which keys must be present?"* and both are answered by the same function, so the
113 rows already in the database are enforced **without a data migration** — a migration would
have had to rewrite rows to express something the field already said.

Everything else in the schema is **documentation** and is left alone. There is no JSON Schema
validator here and inventing one would be a large piece of machinery to answer a question this
column was already asking.

## Why it fails the task rather than warning

A warning is a log line, and the reader of a green stage does not read log lines. The rule has
to be at the boundary where `completed` is granted, because that is the moment the claim is
made. `task_execution._finish` refuses the transition, so the task ends `failed` with the
missing keys named — and a person can retry it, which is a thing they can do about it.

Enforced on `COMPLETED` only. A task that is `paused`, `blocked` or waiting for approval has
not claimed to have finished, and failing it for missing output would make the whole process
unrunnable.
"""

from __future__ import annotations

from typing import Any

#: The key that means "these keys are required", in its explicit form.
REQUIRED = "required"
#: ... and in the form the existing rows use.
PRODUCES = "produces"


def required_keys(schema: dict[str, Any] | None) -> tuple[str, ...]:
    """Which output keys this task said it would produce.

    Empty when the task declared nothing, which is most tasks: an `execution` that returns
    whatever it returns has no contract, and inventing one for it would be noise.
    """
    if not schema:
        return ()
    if isinstance(schema.get(REQUIRED), (list, tuple)):
        return tuple(str(k) for k in schema[REQUIRED] if str(k).strip())
    if isinstance(schema.get(PRODUCES), str):
        return tuple(
            part.strip() for part in schema[PRODUCES].replace(";", ",").split(",") if part.strip()
        )
    if isinstance(schema.get(PRODUCES), (list, tuple)):
        return tuple(str(k) for k in schema[PRODUCES] if str(k).strip())
    return ()


def missing_keys(output: dict[str, Any] | None, schema: dict[str, Any] | None) -> tuple[str, ...]:
    """The declared keys the output does not have. Empty means the contract was met.

    `output is None` counts as producing nothing, which is the honest reading: a task that
    declared an output and returned nothing has not produced it.
    """
    needed = required_keys(schema)
    if not needed:
        return ()
    present = set(output or {})
    return tuple(k for k in needed if k not in present)


def describe_mismatch(output: dict[str, Any] | None, schema: dict[str, Any] | None) -> str:
    """A sentence a person can act on, naming what was required and what arrived.

    Both halves, because "it did not do what it said" without saying which part is the kind of
    message that gets a task retried without anybody looking at why.

    **And a third case, which is not a failure of the department.** A value the model
    began and did not finish is a *platform* fault — the answer was cut off in transit,
    not withheld — and it is reported as such. Measured on a real procurement run: a
    department produced exactly the right answer and it was truncated, and the message
    said `produced nothing`. That is a confident, specific and wrong statement about
    work that was done, and it sends the review loop to rerun the same work forever.
    """
    needed = required_keys(schema)
    present = sorted(output or {})
    if present and all(_is_truncated(v) for v in (output or {}).values()):
        return (
            f"this task said it would produce {', '.join(needed)}, and began "
            f"{', '.join(present)} but every value was cut off before it finished. "
            "This is a runtime fault, not a department that declined to answer: the "
            "answer was truncated, so the task is recorded as failed and the keys it did "
            "not reach are not counted as produced"
        )
    if present and any(_is_truncated(v) for v in (output or {}).values()):
        cut = sorted(k for k, v in (output or {}).items() if _is_truncated(v))
        return (
            f"this task said it would produce {', '.join(needed)}, and produced "
            f"{', '.join(present)} — but {', '.join(cut)} was cut off mid-answer. "
            "The missing part is a runtime limit, not missing work"
        )
    return (
        f"this task said it would produce {', '.join(needed)}, and produced "
        f"{', '.join(present) if present else 'nothing'}. A task that does not produce what "
        "it declared has not done the work, so it is recorded as failed rather than finished"
    )


def _is_truncated(value: Any) -> bool:
    """True for a value the runtime knows was cut off.

    Duck-typed on the marker rather than imported from the runtime, because this is
    `domain/` and it may not import upward. The marker is a `str` subclass carrying a
    `truncated` property, and anything else — including a plain string the model wrote
    itself — is not truncated.
    """
    return getattr(value, "truncated", False) is True


__all__ = ["PRODUCES", "REQUIRED", "describe_mismatch", "missing_keys", "required_keys"]
