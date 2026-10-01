"""Whether a department's answer is an answer, decided without asking a model.

The middle tier of this organisation is a manager, and a manager who cannot say
"this is not good enough" is not managing. So the office has to be able to read
what a department produced and reject it. That is the loop the brief asks for and
the one the playbook's agent dossier calls for when it lists *output quality
criteria* as a required field of every agent.

**Why half of this is a pure function and not a second model call.**

Judging "is this a good contract risk review" needs a model. Judging "did the run
produce the four things it said it would produce, and are any of them a
placeholder" does not, and the part that does not is the part that keeps failing.
Measured on this project: a run declared `approved_headcount` and wrote
`{"scripted": true, "proposal_count": 0}`, and a scripted runtime's own output
carried `[draft] <key>` for every value. A model reviewer handed those would have
to notice, and a model that notices is a model that sometimes does not. The
deterministic checks below are the ones that must never be negotiable, and they
run first; only if they pass is a judgement worth asking for.

**Why reruns are bounded here rather than at the call site.** A manager who
rejects forever is not managing either, and a loop with no bound is a way to spend
a budget without producing anything. The bound is part of the decision, so it
belongs in the decision.

No I/O, no clock, no sibling imports: this is a rule about a value, and it is
tested as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Values that mean "nothing was produced". Compared case-insensitively against a
#: stripped, lowercased copy of each value.
#:
#: The markers are here because a placeholder is the failure mode that looks most
#: like success. A task whose output is `{"verdict": "[draft] verdict"}` satisfies
#: every "did you produce the key" check ever written, and it is the single most
#: likely way this platform marks unfinished work as done -- it happened here, in
#: the scenario harness, and was only caught by looking at the values.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "[draft]",
    "todo",
    "tbd",
    "to be determined",
    "to be confirmed",
    "xxx",
    "lorem ipsum",
    "placeholder",
    "n/a",
    "not available",
    "chưa xác định",
    "tbd_",
    "<fill",
    "{{",
    "...",
)

#: Below this, a text value is a label rather than an answer. Calibrated against
#: the shortest real answers in the scenario catalogue: a procurement winner with
#: its reason is comfortably over it, and "N/A" or "ok" is comfortably under.
MIN_TEXT_LENGTH = 12

#: A structured value (list or dict) must have at least this many entries to be
#: evidence of work. A one-key dict is a restatement of the question.
MIN_ITEMS = 1

#: How many times a piece of work may be sent back before the office stops
#: sending it back and escalates instead.
#:
#: Two, not one and not five. One means a department that is consistently wrong
#: gets a single retry and then a bad answer reaches the executive. Five means a
#: department stuck in a loop burns the whole budget and still produces nothing.
DEFAULT_MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class Check:
    """One question asked of an output, and what it answered."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    """The office's finding on one piece of work.

    `ok` is not a vibe: it is every check passing. `findings` is written to be
    shown to a person and handed to the department being sent back, so it names
    the key and says what was wrong with it rather than reporting a count.
    """

    ok: bool
    checks: tuple[Check, ...] = ()
    findings: tuple[str, ...] = ()

    def as_text(self) -> str:
        """The review, in the words a manager would use.

        Used verbatim as the rerun's brief, so it has to be specific enough to act
        on. "Failed 2 checks" is not; "verdicts: contains a placeholder value" is.
        """
        if self.ok:
            return "Accepted: every check passed."
        lines = ["Rejected. The following must be fixed:"]
        lines.extend(f"- {finding}" for finding in self.findings)
        return "\n".join(lines)


def _is_placeholder(value: Any) -> bool:
    """Whether this value is a stand-in rather than an answer."""
    text = str(value).strip().lower()
    if not text:
        return True
    return any(marker in text for marker in PLACEHOLDER_MARKERS)


def _substantive(value: Any) -> tuple[bool, str]:
    """Whether a value carries enough to be worth reading."""
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) < MIN_TEXT_LENGTH:
            return False, f"only {len(stripped)} characters"
        return True, ""
    if isinstance(value, (list, tuple, set, dict)):
        if len(value) < MIN_ITEMS:
            return False, "empty"
        return True, ""
    if isinstance(value, (int, float, bool)):
        # A bare number is a legitimate answer to "how many", so it passes. It is
        # rejected only when it is the *only* thing produced, which the presence
        # check catches separately.
        return True, ""
    return True, ""


#: How much of a value's own wording may also appear in the task it was answering
#: before it counts as an echo rather than an answer.
#:
#: Measured against the scenario catalogue, a real finding names the claims, the
#: amounts and the policy, and shares almost nothing with the brief. A restatement
#: shares nearly all of it. 0.85 sits above "mentions the subject" and below
#: "parrots it".
ECHO_OVERLAP = 0.85

#: Below this many distinct words a value is too short for overlap to mean anything,
#: and `"approve"` against a brief that happens to contain "approve" is not an echo.
ECHO_MIN_WORDS = 4

#: Below this many characters the task text is too short to be echoed meaningfully.
ECHO_MIN_SOURCE = 24


def _words(text: str) -> set[str]:
    """Distinct lowercased words, ignoring punctuation.

    Deliberately crude. The question is "did the agent answer, or repeat the brief",
    and repetition survives normalisation: case, accents, spacing and punctuation
    all change while the words do not.
    """
    import re

    return {w for w in re.split(r"[^\w]+", text.lower(), flags=re.UNICODE) if w}


def _echo(value: Any, source_words: set[str]) -> bool:
    """Whether this value is the task handed back rather than an answer to it.

    Measured on a structured value as a whole rather than key by key, because the
    shape of an echo is the whole thing: asked to review three claims, an agent will
    sometimes return `[{"claim": "the three claims"}, ...]`, and judging each string
    alone would miss that every one of them is the brief again.
    """
    if isinstance(value, dict):
        text = " ".join(str(v) for v in value.values())
    elif isinstance(value, (list, tuple, set)):
        text = " ".join(str(v) for v in value)
    elif isinstance(value, str):
        text = value
    else:
        return False

    value_words = _words(text)
    if len(value_words) < ECHO_MIN_WORDS:
        return False
    return len(value_words & source_words) / len(value_words) >= ECHO_OVERLAP


def required_keys(schema: Any) -> tuple[str, ...]:
    """The keys an output was promised, from whatever shape the contract uses.

    Two shapes exist in this codebase and both are live: `{"required": [...]}`
    written by the scenario catalogue and the task API, and `{"produces": "..."}`
    written by the hiring seeder. Reading only the first is how a stage declared
    `approved_headcount` and was then judged against nothing.
    """
    if not isinstance(schema, dict):
        return ()
    listed = schema.get("required")
    if isinstance(listed, (list, tuple)):
        return tuple(str(k) for k in listed)
    produces = schema.get("produces")
    if isinstance(produces, (list, tuple)):
        return tuple(str(k) for k in produces)
    if isinstance(produces, str) and produces:
        return (produces,)
    return ()


def assess_output(
    *,
    output: Any,
    expected_output_schema: Any = None,
    attempt: int = 1,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    source_text: str = "",
) -> ReviewVerdict:
    """Decide whether this output can be sent to the executive.

    `attempt` is 1 for the first try. It is not used to decide *whether* the work
    is good -- a second attempt is judged exactly as strictly as the first, or a
    department learns that retrying is enough -- only whether a rejection may be
    turned into another run.

    `source_text` is the brief the task was given (goal, title, or the `brief` the
    delegation carried). It is optional, and omitting it costs one check rather than
    correctness -- but it is the only way to catch the answer that is the question
    again, which every other check here passes.
    """
    checks: list[Check] = []
    findings: list[str] = []

    wanted = required_keys(expected_output_schema)
    source_words = _words(source_text) if len(source_text.strip()) >= ECHO_MIN_SOURCE else set()

    if output is None:
        checks.append(Check("produced", False, "the run wrote no output at all"))
        return ReviewVerdict(ok=False, checks=tuple(checks), findings=("no output was produced",))

    if isinstance(output, dict) and not output:
        # **An empty mapping is not an answer**, whatever the contract says.
        #
        # Found on a real run, and it is the worst failure this module could have:
        # a free model produced `{}` for every field it was supposed to produce,
        # and the office accepted it three times. The reason is that with no
        # promised keys the checks below have nothing to look at, and a loop over
        # nothing passes. So the row read "no missing keys, nothing to check,
        # clean" and the pipeline reported the work done.
        #
        # `{}` and `None` are the same answer. The check below treats them alike;
        # this is the one that stops it being the *accepted* one.
        checks.append(Check("produced", False, "the run wrote an empty output: {}"))
        return ReviewVerdict(
            ok=False,
            checks=tuple(checks),
            findings=(
                "the run finished with an empty output. It promised "
                f"{list(wanted) or 'a result'} and produced nothing at all, which is "
                "not a short answer -- it is no answer.",
            ),
        )

    if not isinstance(output, dict):
        text = str(output).strip()
        if not text:
            checks.append(Check("produced", False, "empty"))
            return ReviewVerdict(
                ok=False, checks=tuple(checks), findings=("no output was produced",)
            )
        # A contract that promised keys cannot be met by prose, so this is only
        # acceptable when nothing specific was promised.
        if wanted:
            checks.append(Check("keys", False, f"promised {list(wanted)}, got text"))
            findings.append(f"the task promised {list(wanted)} but returned a paragraph")
            return ReviewVerdict(ok=False, checks=tuple(checks), findings=tuple(findings))
        checks.append(Check("produced", True))
        return ReviewVerdict(ok=True, checks=tuple(checks))

    missing = [key for key in wanted if key not in output]
    checks.append(
        Check(
            "keys",
            not missing,
            f"missing {missing}" if missing else f"all {len(wanted)} present",
        )
    )
    if missing:
        findings.append(
            f"these were promised and are absent: {', '.join(missing)}. Produce every one of them."
        )

    # Judge the promised keys hardest, and the extras too: a department that
    # answers what was asked and pads the rest with noise has still not done the
    # work, and the executive reads the padding first.
    for key in sorted(output):
        value = output[key]
        if _is_placeholder(value):
            checks.append(Check(f"placeholder:{key}", False, str(value)[:60]))
            findings.append(
                f"'{key}' is a placeholder, not an answer: {str(value)[:80]!r}. "
                f"Put the real finding there."
            )
            continue
        if source_words and _echo(value, source_words):
            checks.append(Check(f"echo:{key}", False, "restates the task it was given"))
            findings.append(
                f"'{key}' repeats the task back rather than answering it: {str(value)[:80]!r}. "
                f"Answer it -- name the specific claims, figures and decision."
            )
            continue
        substantive, why = _substantive(value)
        checks.append(Check(f"substantive:{key}", substantive, why))
        if not substantive:
            findings.append(f"'{key}' is too thin to review ({why}). Give the finding itself.")

    ok = all(check.passed for check in checks)
    return ReviewVerdict(ok=ok, checks=tuple(checks), findings=tuple(findings))


def should_rerun(
    verdict: ReviewVerdict,
    *,
    attempt: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[bool, str]:
    """Whether to send this back, and why -- or why not to.

    Three outcomes, not two: accept, send back, or **stop sending back**. The third
    is the one that matters and is the one a boolean cannot express. A department
    that has been sent the same work back `max_attempts` times has told the office
    something real -- it cannot do this work, or cannot do it with what it has --
    and the honest response is to escalate to the executive rather than to keep
    asking.
    """
    if verdict.ok:
        return False, "the work passed review"
    if attempt >= max_attempts:
        return False, (
            f"it has been sent back {attempt} times and still does not meet the "
            f"contract; escalating instead of asking again"
        )
    return True, f"sent back for attempt {attempt + 1} of {max_attempts}"


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "ECHO_MIN_SOURCE",
    "ECHO_MIN_WORDS",
    "ECHO_OVERLAP",
    "MIN_TEXT_LENGTH",
    "PLACEHOLDER_MARKERS",
    "Check",
    "ReviewVerdict",
    "assess_output",
    "required_keys",
    "should_rerun",
]
