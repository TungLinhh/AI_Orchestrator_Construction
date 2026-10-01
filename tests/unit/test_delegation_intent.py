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


class TestTheKeyCatchesARestatement:
    @pytest.mark.parametrize(
        ("original", "restated"),
        [(ONE, ONE_RESTATED), (THREE, THREE_RESTATED)],
        ids=["short-objective", "long-objective"],
    )
    def test_appending_a_clause_does_not_move_the_key(self, original: str, restated: str) -> None:
        """The measured failure: the model re-asked the same work with a clause appended.

        Four times on one parent, and nothing stopped it. A key that moves when a clause is
        appended is not a key for "the same instruction".
        """
        assert key(original) == key(restated)


class TestTheKeyDoesNotOverreach:
    def test_two_different_jobs_stay_different(self) -> None:
        """**The false-positive this design is most at risk of, and it is a real one.**

        These two share their first fifteen tokens -- same project, same verb -- and diverge
        only at *token 16*. A prefix of 15 or fewer would refuse one of them, which is refusing
        real delegation. The test exists to fail if the constant is ever shortened.
        """
        assert key(ONE) != key(THREE)

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
