from observability.context import (
    ObservabilityContext,
    bind_context,
    current_context,
    new_request_context,
    normalize_correlation_id,
)


def test_new_request_context_generates_request_id_and_preserves_valid_values():
    context = new_request_context(
        request_id="req-123",
        session_id="session:abc",
        research_id="research_456",
    )

    assert context.request_id == "req-123"
    assert context.session_id == "session:abc"
    assert context.research_id == "research_456"
    assert context.run_id is None


def test_invalid_external_correlation_id_is_replaced():
    normalized = normalize_correlation_id("contains spaces and\ncontrol chars")

    assert normalized != "contains spaces and\ncontrol chars"
    assert " " not in normalized
    assert "\n" not in normalized


def test_nested_context_binding_restores_parent_context():
    parent = ObservabilityContext(request_id="req-parent", session_id="session-1")

    assert current_context() is None
    with bind_context(parent):
        assert current_context() == parent
        with bind_context(run_id="run-child", research_id="research-1"):
            child = current_context()
            assert child is not None
            assert child.request_id == "req-parent"
            assert child.session_id == "session-1"
            assert child.run_id == "run-child"
            assert child.research_id == "research-1"
        assert current_context() == parent
    assert current_context() is None

