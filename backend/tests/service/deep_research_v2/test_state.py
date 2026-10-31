from service.deep_research_v2.state import ResearchPhase, create_initial_state


def test_state_exposes_outline_approval_phase():
    assert (
        ResearchPhase.AWAITING_OUTLINE_APPROVAL.value
        == "awaiting_outline_approval"
    )


def test_initial_state_has_no_outline_revision():
    state = create_initial_state("topic", "session-1")

    assert state["phase"] == "init"
    assert state["outline_revision"] is None
    assert state["quality_gate_passed"] is False
    assert state["quality_gate_reasons"] == []
    assert state["review_history"] == []
