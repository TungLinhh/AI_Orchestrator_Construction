"""Scanning proposed text for the things a proposal must never contain.

`SELF_IMPROVEMENT.md` §5 gate 3. A proposal is the most attractive place in this
system to hide an instruction, for a reason that has nothing to do with malice: a
proposal is *supposed* to contain text that changes how an agent behaves. "Collect
the cost centre first" and "collect the API key first" are the same shape of
sentence. The channel and the payload are identical, so nothing about the structure
distinguishes them — which is why the content has to be inspected.

**Every field is scanned, not just the content.** A payload in `why` is read by
every human who reviews the packet. A payload in `falsifier` is read by whatever
generates the *next* proposal, which means it executes on a future run without
anyone having read it. An injection in `old_string` is a payload aimed at the diff
viewer, who is skimming. `new_string` is the one that ships.

Three classes, because they fail differently and the operator needs to know which:

* **injection** — text that tries to change the instructions of whatever reads it.
* **exfiltration** — text that tries to move a credential somewhere.
* **hidden** — text that is invisible to a human reviewer but not to a model. The
  dangerous one is the Unicode tag block (U+E0000 to U+E007F), which is literally
  designed to encode a prompt that renders as nothing, and which has been used
  against exactly this kind of system.

A pure function with no I/O, so it can be tested exhaustively and so that
"the scanner ran" is a claim about code rather than about a service being up.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from ai_orchestrator.domain.learning import ProcedureProposal


class Danger(StrEnum):
    INJECTION = "injection"
    EXFILTRATION = "exfiltration"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing wrong, and where it was.

    `field` matters: "injection in `new_string`" and "injection in `why`" are the
    same class of danger with completely different reach, and a review packet that
    does not say which is not actionable.
    """

    danger: Danger
    field: str
    detail: str
    #: The matched text, truncated. Enough to identify it, not enough to become a
    #: payload in the operator's own log viewer.
    evidence: str

    def as_row(self) -> dict[str, str]:
        return {
            "danger": str(self.danger),
            "field": self.field,
            "detail": self.detail,
            "evidence": self.evidence,
        }


#: Which form of the text a pattern is matched against.
#:
#: Three, because one is not enough and two is not either. `RAW` for values whose
#: punctuation is the signal — a DSN and an API key are mostly punctuation, and
#: normalising it away means the pattern can never match. `NORMALISED` for
#: punctuation-as-separator, so a dashed phrase reads as words. `DEOBFUSCATED` for
#: the rest, because `i g n o r e previous instructions` is the cheapest
#: obfuscation there is and it is exactly the thing a scanner exists to catch.
RAW = "raw"
NORMALISED = "normalised"
DEOBFUSCATED = "deobfuscated"

# --- injection ---------------------------------------------------------------
# Phrases that try to re-address whatever is reading the text. Matched on a
# normalised form (lowercased, whitespace collapsed, punctuation spaced out) so
# that "i g n o r e" and "Ignore  previous" are the same string, because an
# attacker who expects a scanner will try exactly that.

_INJECTION_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"\b(ignore|disregard|forget|discard)\b[^.]{0,40}\b"
            r"(previous|prior|above|earlier|all|any|your|the)\b[^.]{0,20}\b"
            r"(instruction|instructions|prompt|prompts|rule|rules|directive|"
            r"directives|guideline|guidelines|context)\b"
        ),
        "asks the reader to ignore instructions it was given",
        DEOBFUSCATED,
    ),
    (
        re.compile(
            r"\b(you are now|from now on|new instructions?|updated instructions?|"
            r"system prompt|override your|overrule)\b"
        ),
        "re-addresses the reader or asks it to override its instructions",
        DEOBFUSCATED,
    ),
    (
        re.compile(
            r"(^|\n)\s*(system|assistant|developer|human)\s*:|<\|(im_start|im_end|"
            r"system|endoftext)\|>|\[/?INST\]|###\s*(system|instruction)"
        ),
        "contains chat-template or role markers that fake a turn boundary",
        RAW,
    ),
    (
        re.compile(
            r"\b(reveal|print|show|repeat|output|echo)\b[^.]{0,30}\b"
            r"(your|the)\b[^.]{0,20}\b(prompt|instructions|system message|"
            r"rules|configuration)\b"
        ),
        "asks the reader to disclose its own instructions",
        DEOBFUSCATED,
    ),
    (
        re.compile(
            r"\b(do not|don't|never)\b[^.]{0,30}\b(mention|reveal|disclose|"
            r"tell)\b[^.]{0,30}\b(this|it|the user|the operator)\b"
        ),
        "tells the reader to conceal what it is doing",
        DEOBFUSCATED,
    ),
)

# --- exfiltration ------------------------------------------------------------

#: Words that *name* a credential. This is a vocabulary for finding one, not a
#: credential, and the two are far enough apart in meaning that naming it after
#: what it finds rather than what it is keeps that obvious to the next reader.
_CREDENTIAL_WORD = (
    r"(api[ _-]?key|secret[ _-]?key|access[ _-]?key|private[ _-]?key|auth[ _-]?token|"
    r"bearer[ _-]?token|password|passwd|credential|credentials|\.env\b|environ|"
    r"secret|token)"
)
_MOVEMENT_WORD = (
    r"(send|post|email|e-?mail|upload|exfiltrate|transmit|forward|publish|curl|"
    r"wget|fetch|http|https|webhook|pastebin|requestbin)"
)

_EXFILTRATION_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(rf"\b{_MOVEMENT_WORD}\b[^.]{{0,60}}\b{_CREDENTIAL_WORD}\b", re.IGNORECASE),
        "moves a credential somewhere: a movement verb next to a secret noun",
        DEOBFUSCATED,
    ),
    (
        re.compile(rf"\b{_CREDENTIAL_WORD}\b[^.]{{0,60}}\b{_MOVEMENT_WORD}\b", re.IGNORECASE),
        "moves a credential somewhere: a secret noun next to a movement verb",
        DEOBFUSCATED,
    ),
    (
        # A DSN with an inline password. This is the exact shape of the value that
        # was found committed in another repository on this machine, so it is
        # matched as a literal pattern rather than described. Matched against the
        # *raw* text: `postgres://user:pass@` is punctuation, and the normaliser
        # spaces punctuation out, so a DSN run through it looks like
        # `postgres : / / user : pass @` and no such pattern can ever match.
        re.compile(
            r"\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://"
            r"[^\s:@/]+:[^\s@/]+@"
        ),
        "contains a connection string with an inline password",
        RAW,
    ),
    (
        re.compile(
            r"\b(read|cat|open|print|dump|list)\b.{0,20}?"
            r"(\.env\b|/proc/self/environ|~/\.aws/credentials|\.ssh/id_[a-z0-9]+|\.netrc)",
            re.IGNORECASE,
        ),
        "reads a file whose contents are credentials",
        RAW,
    ),
    (
        # A real key shape, whatever the field it sits in. `sk-` + 20+ alphanumerics
        # is the shape used by the provider this project talks to. Raw, for the same
        # reason as the DSN: the prefix is punctuation.
        re.compile(
            r"(?<![\w-])(sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|sk-or-v1-[A-Za-z0-9]{16,})"
        ),
        "contains something shaped like a live credential",
        RAW,
    ),
)


# --- hidden ------------------------------------------------------------------
# Characters that render as nothing, as something else, or as nothing to a human
# while remaining perfectly legible to a model.

#: Zero-width characters, as codepoints rather than as literal characters.
#:
#: Written numerically because a literal is invisible. An editor that normalises
#: one, a copy-paste that drops one, or a review that cannot see one will silently
#: remove a check. The first draft of this set was typed as a string literal and
#: came out five characters instead of seven, missing U+200B -- the most common of
#: them -- with nothing anywhere to indicate that.
_ZERO_WIDTH_CODES = frozenset(
    {
        0x200B,  # zero width space
        0x200C,  # zero width non-joiner
        0x200D,  # zero width joiner
        0x2060,  # word joiner
        0xFEFF,  # zero width no-break space, and a BOM when it appears alone
        0x00AD,  # soft hyphen
        0x180E,  # mongolian vowel separator
    }
)
_ZERO_WIDTH = frozenset(chr(code) for code in _ZERO_WIDTH_CODES)

#: Bidirectional overrides and isolates. A different failure from invisible text:
#: this one *renders*, as a different string from the one it contains.
_BIDI_OVERRIDE = frozenset(
    chr(code) for code in (set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)))
)

#: The Unicode tag block. Not a character: a *range* reserved for encoding text
#: that renders as nothing, which makes it the standard carrier for a hidden
#: instruction. Numeric for the same reason as `_ZERO_WIDTH_CODES` — a literal in
#: this range is invisible in an editor and in a diff.
_UNICODE_TAG_START, _UNICODE_TAG_END = 0xE0000, 0xE007F

_PRIVATE_USE = (0xE000, 0xF8FF)


def _hidden_findings(text: str, field: str) -> list[Finding]:
    """Invisible text, found by inspecting code points.

    Not by pattern: the Unicode tag block has no regex that is worth writing, and
    the bidi overrides are the reason a visible string can *read* as one thing and
    *be* another. A human reviewing a diff sees the rendering; a model sees the
    code points. That gap is the attack.
    """
    found: list[Finding] = []
    for index, char in enumerate(text):
        code = ord(char)
        if char in _ZERO_WIDTH:
            found.append(
                Finding(
                    danger=Danger.HIDDEN,
                    field=field,
                    detail="zero-width character: invisible to a reviewer, not to a model",
                    evidence=f"U+{code:04X} at offset {index}",
                )
            )
        elif char in _BIDI_OVERRIDE:
            found.append(
                Finding(
                    danger=Danger.HIDDEN,
                    field=field,
                    detail="bidirectional override: the text renders differently from what it says",
                    evidence=f"U+{code:04X} at offset {index}",
                )
            )
        elif _UNICODE_TAG_START <= code <= _UNICODE_TAG_END:
            found.append(
                Finding(
                    danger=Danger.HIDDEN,
                    field=field,
                    detail=(
                        "Unicode tag character: encodes text that renders as nothing, "
                        "which is the standard carrier for a hidden instruction"
                    ),
                    evidence=f"U+{code:05X} at offset {index}",
                )
            )
        elif _PRIVATE_USE[0] <= code <= _PRIVATE_USE[1]:
            found.append(
                Finding(
                    danger=Danger.HIDDEN,
                    field=field,
                    detail="private-use character: renders as a box or nothing",
                    evidence=f"U+{code:04X} at offset {index}",
                )
            )
        if len(found) >= 8:
            # A bounded number. A payload that repeats the same character a
            # thousand times should not produce a thousand findings; the operator
            # needs to know it is bad, not how bad.
            return found[:8]
    if "<!--" in text and "-->" in text:
        found.append(
            Finding(
                danger=Danger.HIDDEN,
                field=field,
                detail="HTML comment: a reviewer reading rendered text does not see it",
                evidence="<!-- ... -->",
            )
        )
    return found


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, and space out punctuation.

    The spacing is the point. `ignore-previous-instructions` and
    `ignore previous instructions` are the same instruction, and matching only the
    spaced form is matching only what a careless attacker would send.
    """
    lowered = text.lower()
    spaced = re.sub(r"([^\w\s])", r" \1 ", lowered)
    return re.sub(r"\s+", " ", spaced)


#: A word written one letter at a time: `i g n o r e`. The cheapest obfuscation
#: there is, and the one that separates a scanner from a string search.
_SPACED_LETTERS = re.compile(r"(?<![a-z])((?:[a-z][\s]+){2,}[a-z])(?![a-z])")


def _despace_letters(text: str) -> str:
    """Join letters that were written with spaces between them.

    Only runs of three or more single characters, and only where a longer word is
    not already present on either side. That is what makes `i g n o r e` collapse
    while `a b testing` does not — an over-eager version would rewrite ordinary
    prose into nonsense and match on the nonsense.
    """
    return _SPACED_LETTERS.sub(lambda m: re.sub(r"\s+", "", m.group(1)), text)


def scan_text(text: str, field: str) -> list[Finding]:
    """Every problem in one field, in all three classes."""
    if not text or not text.strip():
        return []
    findings: list[Finding] = []
    # NFKC first, so a fullwidth or compatibility form of a letter matches the
    # pattern that expects the plain one.
    normalised = _normalise(unicodedata.normalize("NFKC", text))
    deobfuscated = _despace_letters(normalised)
    forms = {NORMALISED: normalised, DEOBFUSCATED: deobfuscated, RAW: text}
    for danger, patterns in (
        (Danger.INJECTION, _INJECTION_PATTERNS),
        (Danger.EXFILTRATION, _EXFILTRATION_PATTERNS),
    ):
        for pattern, detail, form in patterns:
            match = pattern.search(forms[form])
            if match:
                findings.append(
                    Finding(
                        danger=danger,
                        field=field,
                        detail=detail,
                        evidence=_clip(match.group(0)),
                    )
                )
    findings.extend(_hidden_findings(text, field))
    return findings


def _clip(value: str, limit: int = 60) -> str:
    collapsed = re.sub(r"\s+", " ", value).strip()
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."


#: Every field a proposal carries, in the order a reviewer would read them. A
#: payload in any of them is a payload.
PROPOSAL_FIELDS: tuple[str, ...] = (
    "change.old_string",
    "change.new_string",
    "change.content",
    "change.target",
    "why",
    "falsifier",
    "proposed_by",
)


def _proposal_text(proposal: ProcedureProposal, field: str) -> str:
    if field == "change.old_string":
        return proposal.change.old_string
    if field == "change.new_string":
        return proposal.change.new_string
    if field == "change.content":
        return proposal.change.content
    if field == "change.target":
        return proposal.change.target
    if field == "why":
        return proposal.why
    if field == "falsifier":
        return proposal.falsifier
    if field == "proposed_by":
        return proposal.proposed_by
    return ""


def scan_proposal(proposal: ProcedureProposal) -> list[Finding]:
    """Everything wrong with a proposal, across every field it carries.

    The field list is explicit rather than derived from the model, so a field added
    later is **not** scanned until somebody adds it here — which fails visibly, in
    review, instead of silently. A `dataclasses.asdict` would have covered the new
    field and covered the new attack with it.
    """
    findings: list[Finding] = []
    for field in PROPOSAL_FIELDS:
        findings.extend(scan_text(_proposal_text(proposal, field), field))
    return findings


def verdict(findings: list[Finding]) -> str | None:
    """`None` when clean, and a sentence when not.

    The signature the gate expects: `None` means clean, and there is no way to
    confuse "clean" with "not scanned", because a caller that never called this at
    all has nothing to pass.
    """
    if not findings:
        return None
    kinds = sorted({str(f.danger) for f in findings})
    fields = sorted({f.field for f in findings})
    return (
        f"{len(findings)} finding(s) [{', '.join(kinds)}] in {', '.join(fields)}; "
        "the proposal is quarantined and will not be shown to a reviewer"
    )


def scan_fn(proposal: ProcedureProposal) -> str | None:
    """A `Scan` for `ProposalGate`: the verdict, or `None`.

    Named to be passed as `scan=scan_fn`, which makes the gate's dependency on it
    a single readable token at the call site.
    """
    return verdict(scan_proposal(proposal))


__all__ = [
    "PROPOSAL_FIELDS",
    "Danger",
    "Finding",
    "scan_fn",
    "scan_proposal",
    "scan_text",
    "verdict",
]
