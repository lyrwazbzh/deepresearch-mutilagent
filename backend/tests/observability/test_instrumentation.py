from prometheus_client import CollectorRegistry, generate_latest

from observability.events import bind_event_recorder
from observability.instrumentation import observe_retrieval, observe_tool
from observability.metrics import ApplicationMetrics


async def test_retrieval_decorator_records_success_metrics_and_event():
    metrics = ApplicationMetrics(registry=CollectorRegistry())
    events = []

    @observe_retrieval("web", metrics=metrics)
    async def search():
        return [{"url": "https://example.com"}]

    with bind_event_recorder(lambda **event: events.append(event)):
        result = await search()

    exposition = generate_latest(metrics.registry).decode()
    assert len(result) == 1
    assert 'outcome="success",source="web"' in exposition
    assert events[0]["event_type"] == "retrieval.completed"
    assert events[0]["payload"]["result_count"] == 1


async def test_tool_decorator_classifies_unsuccessful_result():
    metrics = ApplicationMetrics(registry=CollectorRegistry())
    events = []

    @observe_tool("python", metrics=metrics)
    async def execute():
        return {"success": False, "error": "syntax"}

    with bind_event_recorder(lambda **event: events.append(event)):
        result = await execute()

    exposition = generate_latest(metrics.registry).decode()
    assert result["success"] is False
    assert 'outcome="failure",tool="python"' in exposition
    assert events[0]["status"] == "error"
    assert events[0]["payload"]["success"] is False
