"""Read an approved run out of the log, and write down what it teaches.

This is the seam between `ApprovalService` and the skill store: it takes the
approval row a person just granted, reconstructs the run from
`audit_logs`, and produces one proposed `SkillVersion`.

The three decisions that matter here, and why:

* **The log, not the agent's summary.** `audit_logs` holds the tool sequence and
  the platform's refusals as recorded, including the ones the agent would not
  have mentioned. The agent's own account of its run is the thing under
  examination; using it as the evidence would be marking its own homework.

* **Proposed, never applied.** A `SkillVersion` row is what the runtime loads on
  the next run, so writing one is applying it. The version is therefore written
  with `derived_from` carrying the evidence, and the activation is a separate
  step a person takes. The alternative is an agent that rewrites its own
  instructions after every run, which converges on whatever it already wanted.

* **A failure here is reported, never propagated.** It runs inside a decision
  the person already made. Raising would turn "the platform could not write a
  skill" into "your approval was rolled back", which is a strictly worse outcome
  than not learning.
"""

from __future__ import annotations

from typing import Any

from ai_orchestrator.application.skill_learning import (
    LESSON_EVIDENCE_LIMIT,
    RunLesson,
    compose_lesson,
    lesson_is_worth_writing,
    skill_id_for,
)
from ai_orchestrator.domain.ids import SkillVersionId
from ai_orchestrator.persistence.models import AuditLog, Execution, Skill, SkillVersion, Task


class SkillLearner:
    """Turns one approved run into one proposed skill version."""

    def __init__(self, session: Any, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def learn_from(self, *, task_id: str, agent_id: str) -> dict[str, Any]:
        """One lesson from the run behind `task_id`, or a reason there is not one.

        Returns what it did rather than raising, so the caller can log it and
        carry on. A return value of `{"written": False, ...}` is the normal
        outcome for most runs and is not a failure.
        """
        lesson = await self._lesson_for(task_id)
        if lesson is None:
            return {
                "written": False,
                "reason": "the run left no execution to learn from",
            }

        worth, why = lesson_is_worth_writing(lesson)
        if not worth:
            return {"written": False, "reason": why}

        skill_id = skill_id_for(agent_id, lesson.task_title)
        await self.ensure_skill(
            skill_id=skill_id,
            name=f"learned: {lesson.task_title}",
            description=(
                "Proposed from an approved run. Not published: publishing it is what "
                "makes the runtime load it, and that is a person's decision."
            ),
        )
        previous = await self._instructions_for(skill_id)
        text = compose_lesson(lesson, previous=previous)
        # `skill_versions.version` is a **string** column, not an integer, and
        # the comparison that reads the latest one has to agree with that. Passing
        # an int produced `invalid input for query argument $4: 1 (expected str,
        # got int)` -- caught only by running it, because the model says `str` and
        # the value came from arithmetic on a column that is also `str`.
        version = await self._next_version(skill_id)
        version_id = str(SkillVersionId.create())
        self._session.add(
            SkillVersion(
                id=version_id,
                organization_id=self._org,
                skill_id=skill_id,
                version=version,
                instructions=text,
                # Which agent this habit belongs to goes **into the evidence**, not into a
                # parameter or an instance attribute: publication happens later, through a
                # different endpoint, by a different piece of code. A fact recorded only in
                # the caller's head is a fact nobody can reach when it is needed.
                #
                # The evidence travels with the version. A reviewer asked to
                # approve a lesson can only check it against what the agent did,
                # so storing the lesson without the log makes it unfalsifiable.
                #
                # `derived_from`, **not** `test_results`. The publication gate
                # refuses a version whose `test_results` does not say `passed:
                # true`, so evidence parked there would make every lesson
                # unpublishable -- and the only way to publish one would be to
                # invent a passing test result, which is the artefact a reviewer
                # trusts most. Migration 0028.
                derived_from={**lesson.as_evidence(), "agent_id": agent_id},
                # **Not published.** This is the whole of "proposed, not
                # applied": a published version is what the runtime loads on the
                # next run, so publishing here would be the agent editing its
                # own instructions the moment a person clicked Approve. The
                # version exists for someone to read; publishing is the separate
                # step they take.
                is_published=False,
            )
        )
        await self._session.flush()
        return {
            "written": True,
            "skill_id": skill_id,
            "version": version,
            "version_id": version_id,
            "tools": list(lesson.tools_used),
            "refusals": list(lesson.refusals),
        }

    async def _lesson_for(self, task_id: str) -> RunLesson | None:
        """Rebuild the run from the log, newest execution first.

        **Ordered by `started_at`, not by `attempt`.** Ordering by `attempt` looks
        right and is wrong: `attempt` counts the *task's* attempt, and every run
        after an approval gate shares attempt 1, so a task that stopped at the
        gate and then continued produced two executions both numbered 1 — and
        `limit(1)` returned whichever the database happened to order first, which
        is the gate run. That run called no tools, so the learner correctly
        reported "no approach to improve" and the loop appeared broken.

        The newest execution is the one whose result a person saw. `started_at` is
        the only field that says that reliably.
        """
        execution = (
            (
                await self._session.execute(
                    _select(Execution)
                    .where(
                        Execution.organization_id == self._org,
                        Execution.task_id == task_id,
                    )
                    .order_by(Execution.started_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if execution is None:
            return None

        task = (
            (
                await self._session.execute(
                    _select(Task).where(Task.organization_id == self._org, Task.id == task_id)
                )
            )
            .scalars()
            .first()
        )

        tools, refusals = await self._log_for(execution)
        return RunLesson(
            task_title=str(task.title) if task else str(execution.task_id),
            summary=str(execution.summary or ""),
            tools_used=tools,
            refusals=refusals,
            attempts=int(execution.attempt or 1),
        )

    async def _log_for(self, execution: Execution) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The tool sequence and the refusals, in the order the platform recorded them.

        Read from `audit_logs.context` rather than from a summary field, because
        the sequence is the part a lesson can teach and the refusals are the part
        an agent would least likely include. Both are bounded: a lesson built
        from a thousand rows is a document, and a document is not applied on
        every run.
        """
        rows = (
            (
                await self._session.execute(
                    _select(AuditLog)
                    .where(
                        AuditLog.organization_id == self._org,
                        AuditLog.execution_id == str(execution.id),
                    )
                    .order_by(AuditLog.sequence.asc())
                    .limit(LESSON_EVIDENCE_LIMIT * 8)
                )
            )
            .scalars()
            .all()
        )

        tools: list[str] = []
        refusals: list[str] = []
        for row in rows:
            if row.resource_type == "tool" and row.resource_id:
                tools.append(str(row.resource_id))
            if row.policy_reason:
                refusals.append(str(row.policy_reason))
        return tuple(tools[:LESSON_EVIDENCE_LIMIT]), tuple(refusals[:LESSON_EVIDENCE_LIMIT])

    async def _instructions_for(self, skill_id: str) -> str:
        """The newest instructions this skill already carries, if any.

        Read rather than assumed empty: a second run of the same work should
        extend what the first one learned, and starting from a blank page each
        time throws away the earlier lesson.
        """
        from sqlalchemy import func

        latest = (
            (
                await self._session.execute(
                    _select(func.max(SkillVersion.version)).where(
                        SkillVersion.organization_id == self._org,
                        SkillVersion.skill_id == skill_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if not latest:
            return ""
        row = (
            (
                await self._session.execute(
                    _select(SkillVersion).where(
                        SkillVersion.organization_id == self._org,
                        SkillVersion.skill_id == skill_id,
                        SkillVersion.version == latest,
                    )
                )
            )
            .scalars()
            .first()
        )
        return str(row.instructions) if row else ""

    async def _next_version(self, skill_id: str) -> str:
        from sqlalchemy import func

        highest = (
            (
                await self._session.execute(
                    _select(func.max(SkillVersion.version)).where(
                        SkillVersion.organization_id == self._org,
                        SkillVersion.skill_id == skill_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        return str(int(highest or 0) + 1)

    async def ensure_skill(self, *, skill_id: str, name: str, description: str) -> None:
        """The `skills` row a version points at, created if the lesson is the first."""
        existing = (
            (
                await self._session.execute(
                    _select(Skill).where(Skill.organization_id == self._org, Skill.id == skill_id)
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            self._session.add(
                Skill(
                    id=skill_id,
                    organization_id=self._org,
                    name=name[:120],
                    description=description[:400],
                )
            )
            await self._session.flush()


def _select(*args: Any) -> Any:
    from sqlalchemy import select

    return select(*args)
