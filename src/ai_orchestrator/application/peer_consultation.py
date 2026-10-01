"""Asking a colleague a question, as distinct from handing them work.

`delegate_to_agent` transfers ownership: a task row, a delegation row, and
somebody responsible for the outcome. That is the wrong instrument for a
question, and using it for one has consequences the platform cannot undo —
a consultation shows up in the manager's queue as unfinished work, the
recipient's load goes up, and the budget is spent before anyone learns
anything.

So asking is a separate capability with its own rules, and the rules are the
point:

* **A question costs a model call, so there is a cap.** The ceiling is per
  run and enforced by a counter, not by the prompt. A prompt that says "ask
  sparingly" is a request; a counter is a limit. A confused model that calls
  `ask_agent` in a loop is stopped by the counter, and the refusal it reads
  says why.
* **It creates no task and no delegation.** Nothing is owned, nothing is
  queued, and the answer is returned inline to the caller.
* **It is scoped to peers and subordinates.** The roster the caller already
  holds is the only addressable set. An agent cannot ask a superior to do its
  job through a channel the platform does not audit as delegation, which would
  be a way around the whole delegation graph.
* **The answer is text and is not trusted.** The recipient is a different
  agent whose output is untrusted input to the caller, exactly like a retrieved
  document. It is returned as an answer, never executed.

The refusal is the feature. "I could not ask, and here is why" lets the caller
carry on; a silent hang or an unbounded loop does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_orchestrator.domain.contracts import DelegateOption

#: Consultations allowed per run. Small on purpose: the first thing to measure
#: is whether the model asks at all, and a generous ceiling makes that question
#: unanswerable because the bill is already the answer. Raising it needs a
#: distribution, not a feeling -- see F206 on why tool calls are not yet
#: counted per execution.
CONSULT_LIMIT_PER_RUN = 4

#: What a refused consultation says, per cause. A model handed "REFUSED" learns
#: nothing and retries; a model told the ceiling was reached stops and finishes.
REFUSAL_NOT_ADDRESSABLE = "AGENT_NOT_AVAILABLE"
REFUSAL_LIMIT = "CONSULT_LIMIT_REACHED"
REFUSAL_QUESTION_REQUIRED = "INVALID_ARGUMENTS"
REFUSAL_NO_ANSWER = "NO_ANSWER"


@dataclass
class ConsultationLedger:
    """The per-run record of who asked whom, and what they were told.

    Kept in memory for the length of one run. Persisting it would be a second
    source of truth about conversations, and the tool-call log already holds
    every invocation; this exists to enforce the ceiling and to make the refusal
    explainable, not to be a ledger of its own.
    """

    limit: int = CONSULT_LIMIT_PER_RUN
    asked: list[tuple[str, str]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - len(self.asked))

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def record(self, asker: str, target: str) -> None:
        self.asked.append((asker, target))


def can_consult(roster: tuple[DelegateOption, ...], name: str) -> bool:
    """Whether `name` is someone this agent is allowed to ask.

    Addressability is the roster and nothing else. Deriving a separate notion
    of "who may be consulted" from the org tree would be a second, subtly
    different list — the exact failure mode F171 warns about, where a rule
    hardens into a second vocabulary that no longer tracks the first.
    """
    return any(option.agent_name == name for option in roster)


def build_query(task_title: str, question: str, limit: int = 600) -> str:
    """The question as the recipient's agent will see it.

    The recipient is an agent, not a person, and the distinction matters: it is
    told what it is being consulted about rather than left to guess from a bare
    question, and told that nobody is waiting on it forever. A question with no
    context invites an answer about the wrong task, and a question with no
    deadline invites an answer that arrives after the caller has given up.
    """
    question = question.strip()
    title = task_title.strip() or "an unspecified piece of work"
    return (
        f'A colleague is working on "{title}" and needs your judgement.\n\n'
        f"Their question: {question}\n\n"
        f"Answer in at most {limit} characters. State the recommendation first, "
        f"then the one fact that would change it. If the question depends on "
        f"something you cannot see, say what is missing rather than guessing. "
        f"This is a consultation, not a transfer of work: you are not taking this "
        f"on, and no task has been created for you."
    )
