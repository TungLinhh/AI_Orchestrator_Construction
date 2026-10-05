from ai_orchestrator.domain.human_exceptions import WorkClass, approval_class, work_class


def test_routine_drafting_has_no_human_exception():
    assert work_class(effect="prepare") is WorkClass.ROUTINE


def test_doa_and_external_actions_keep_their_reason():
    assert work_class(doa_subject="financial_commitment") is WorkClass.DOA_BAND
    assert work_class(effect="external_send") is WorkClass.IRREVERSIBLE_EXTERNAL
    assert work_class(effect="destructive") is WorkClass.IRREVERSIBLE_EXTERNAL


def test_a_failed_review_budget_is_distinct_from_an_agent_request():
    assert work_class(failed_escalation=True) is WorkClass.FAILED_ESCALATION
    assert work_class(agent_requested=True) is WorkClass.AGENT_REQUEST
    assert (
        approval_class(action_type="procedure.change", effect="mutate_internal", payload={})
        is WorkClass.POLICY_EXCEPTION
    )


def test_ordered_reviews_bind_each_decision_to_its_exact_draft():
    from ai_orchestrator.domain.human_exceptions import pending_human_review

    schema = {
        "properties": {"jd": {"x-human-review-order": 1}, "rubric": {"x-human-review-order": 2}}
    }
    output = {"jd": {"salary": 22}, "rubric": {"quality": 70}, "shortlist": ["Lan"]}
    first = pending_human_review(output=output, schema=schema, approved_outputs=())
    assert first[1] == {"jd": {"salary": 22}}
    second = pending_human_review(output=output, schema=schema, approved_outputs=(first[1],))
    assert second[1] == {"jd": {"salary": 22}, "rubric": {"quality": 70}}
    assert (
        pending_human_review(output=output, schema=schema, approved_outputs=(first[1], second[1]))
        is None
    )
    assert pending_human_review(
        output=output,
        schema=schema,
        approved_outputs=({"rubric": output["rubric"]}, {"jd": output["jd"]}),
    )[1] == {"jd": output["jd"], "rubric": output["rubric"]}
    changed = {**output, "jd": {"salary": 99}}
    assert pending_human_review(
        output=changed, schema=schema, approved_outputs=(first[1], second[1])
    )[1] == {"jd": {"salary": 99}}
    assert pending_human_review(output=output, schema={}, approved_outputs=()) is None
