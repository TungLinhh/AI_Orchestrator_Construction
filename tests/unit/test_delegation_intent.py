"""The intent key, asserted against the five real rows that produced it.

Migration `0026` exists because the CEO queue listed one piece of work four times, and reading
the **titles** said "four duplicates" while reading the **goals** said two *superseded pairs*
(F200): the model re-read its own results and re-asked with an aggregation clause appended.

These are the actual objectives from that parent, kept verbatim. A test built on them fails if
the key stops catching a restatement, starts catching different work, or drifts away from the
constant the migration documents -- which is the failure mode a hash-based key has that nothing
else would notice.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.delegation import INTENT_PREFIX_TOKENS, intent_fingerprint

pytestmark = pytest.mark.unit

ORG = "org_01m3h7j45b6cj3jnphq2ggjteq"
HR = "agt_01m3h7j49ffhdd9wyc8v7w046s"
FINANCE = "agt_01m3h7j4e8kfb3x0qb1l2m3n4o"

#: [1] and [2], verbatim. [2] is [1] with a clause appended.
ONE = (
    "Rà soát hồ sơ bàn giao giai đoạn 1 của Bãi Tràm Estates - kiểm tra chất lượng, đầy đủ "
    "và tuân thủ các hồ sơ bàn giao dự án"
)
ONE_RESTATED = ONE + ", tổng hợp kết quả từ các artifact đã tạo"

#: [3] and [4]. Longer, and [4] appends the same instruction reworded.
THREE = (
    "Rà soát hồ sơ bàn giao giai đoạn 1 của Bãi Tràm Estates - kiểm tra tính đầy đủ, chính "
    "xác, tuân thủ pháp lý và chất lượng của toàn bộ hồ sơ bàn giao dự án giai đoạn 1, bao gồm: "
    "hồ sơ pháp lý, hồ sơ tài chính, hồ sơ kỹ thuật, bản vẽ as-built, biên bản nghiệm thu, "
    "và các giấy phép liên quan"
)
THREE_RESTATED = THREE + ". Tổng hợp kết quả từ các artifact đã tạo trước đó."

#: [5]: the roll-up. Different work, and it completed -- so it must stay insertable.
FIVE = (
    "Rà soát hồ sơ bàn giao giai đoạn 1 của Bãi Tràm Estates - tổng hợp và báo cáo kết quả "
    "kiểm tra chất lượng, đầy đủ và tuân thủ từ các artifact đã tạo trước đó. Tạo báo cáo tổng "
    "hợp cuối cùng cho CEO."
)


def key(goal: str, agent: str = HR) -> str:
    return intent_fingerprint(
        organization_id=ORG, task_type="coordination", goal=goal, owner_agent_id=agent
    )


class TestARestatementIsNoLongerCaughtByTheKey:
    """**This is now the wrong behaviour, deliberately, and the measurement decided it.**

    The key used to be the first 16 tokens of the goal, so "the same work with a clause
    appended" produced the same key and the re-ask was refused. Four re-asks on one parent
    were stopped that way, and it was the only thing stopping them.

    Then a person reported that *a task could not be done while another task was going*, and
    measured the collisions:

    ```
    Chọn nhà thầu cho gói thiết bị điều hòa của dự án Bãi Trầm (lần chạy f648b6e3)
    Chọn nhà thầu cho gói thiết bị điều hòa của dự án Bãi Trầm (lần chạy 21a4b662)
    ```

    Two separate runs of the same tender, refused against each other, because the only
    difference is the run marker the platform appends — and it sits *after* the window. So
    the prefix bought its four refusals by blocking the company's parallelism.

    **The trade, stated once:** a heuristic that fires only on real work is not worth having.
    Missing a duplicate costs one redundant task, and the terminal-refusal work (F263) made
    that cheap. Refusing a real second request costs the company a department's capacity,
    which is what was measured.

    The re-ask is still caught, by a different mechanism and at the right scope: the index is
    now keyed on the parent request, so the *same* parent asking twice collides regardless of
    wording, while two different requests never do
    (`test_task_delegation.py::TestParallelismIsNotBlockedByDuplicateDetection`).
    """

    @pytest.mark.parametrize(
        ("original", "restated"),
        [(ONE, ONE_RESTATED), (THREE, THREE_RESTATED)],
        ids=["short-objective", "long-objective"],
    )
    def test_appending_a_clause_moves_the_key(self, original: str, restated: str) -> None:
        """The restatement is now a *different request*, and must be allowed through."""
        assert key(original) != key(restated)

    def test_the_identical_wording_still_collides(self) -> None:
        """**What must not regress.** Two requests with the same words are still one
        request as far as the key is concerned — this is what still refuses a verbatim
        double-ask, and losing it would be the same failure in the other direction."""
        assert key(ONE) == key(ONE)


class TestTheKeyDoesNotOverreach:
    def test_two_different_jobs_stay_different(self) -> None:
        """**The false-positive this design is most at risk of, and it is a real one.**

        These two share their first fifteen tokens -- same project, same verb -- and diverge
        only at *token 16*. A prefix of 15 or fewer would refuse one of them, which is refusing
        real delegation. The test exists to fail if the constant is ever shortened.

        It also survives the removal of the window, which is the point: the whole-goal hash
        separates them by more than a token, and this is the assertion that says so.
        """
        assert key(ONE) != key(THREE)

    def test_a_run_marker_separates_two_runs(self) -> None:
        """**The measured pair.** Two runs of the same tender, which the prefix refused.

        The run marker is appended by the platform and lands at the end of the goal, so it
        was outside the window that decided they were the same work.
        """
        base = "Chọn nhà thầu cho gói thiết bị điều hòa của dự án Bãi Trầm"
        assert key(f"{base} (lần chạy f648b6e3)") != key(f"{base} (lần chạy 21a4b662)")

    def test_the_same_instruction_to_two_agents_is_two_pieces_of_work(self) -> None:
        assert key(ONE, HR) != key(ONE, FINANCE)

    def test_the_rollup_stays_insertable(self) -> None:
        """[5] is a different task that has already completed; it must never be refused."""
        assert key(FIVE) not in (key(ONE), key(THREE))

    def test_punctuation_and_case_do_not_move_the_key(self) -> None:
        assert key("KIỂM TRA CHẤT LƯỢNG, đầy đủ và tuân thủ") == key(
            "kiểm tra chất lượng đầy đủ và tuân thủ"
        )


class TestTheConstantIsNotDrifting:
    def test_the_length_is_the_one_the_migration_documents(self) -> None:
        """Duplicated between the domain and migration `0026`, so something has to check it.

        A constant that lives in two places and is not compared is two constants. If the
        migration says 16 and the code says 12, the index and the writer disagree and the
        guard is silently inert.
        """
        migration = (
            __import__("pathlib").Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "0026_delegation_intent.py"
        )
        assert f"INTENT_PREFIX_TOKENS = {INTENT_PREFIX_TOKENS}" in migration.read_text(
            encoding="utf-8"
        )

    def test_it_is_long_enough_to_separate_different_work(self) -> None:
        """Derived, not chosen: the two different instructions diverge at 16.

        Asserted as a property so that shortening the constant -- the tempting "simplification"
        -- fails here rather than in production.
        """
        import re

        from ai_orchestrator.domain.delegation import _STOP_WORDS

        def tokens(text: str) -> list[str]:
            clean = re.sub(r"[^\w\s]", " ", text.lower())
            return [t for t in clean.split() if t and t not in _STOP_WORDS]

        a, b = tokens(ONE), tokens(THREE)
        divergence = next(
            (i for i, (x, y) in enumerate(zip(a, b, strict=False), 1) if x != y), None
        )
        assert divergence == INTENT_PREFIX_TOKENS, (
            "the measured divergence point moved, so the constant is stale: the two different "
            f"instructions now diverge at token {divergence}"
        )
        # And the shortest same-work pair must be longer, or the prefix would swallow the
        # clause it is supposed to ignore.
        assert len(a) > INTENT_PREFIX_TOKENS
