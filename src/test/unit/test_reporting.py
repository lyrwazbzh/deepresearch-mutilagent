from __future__ import annotations

from src.agent.identity import canonical_json, content_hash, sha256_hex
from src.agent.schema.report import ReportAst
from src.agent.schema.state import Evidence, ResearchState
from src.agent.reporting.reporting import CitationValidator
from src.agent.task_service import CreateTaskRequest


def task(container, key: str) -> tuple[str, ResearchState]:
    task_id = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="report")
    ).projection.task_id
    return task_id, ResearchState.model_validate(_state(container, task_id))


def _state(container, task_id: str) -> dict:
    from src.agent.backends.persistence.models import ResearchTaskRow

    with container.session_factory() as session:
        return dict(session.get(ResearchTaskRow, task_id).latest_state)


def ast(evidence_id: str, citation_ids: list[str] | None = None) -> ReportAst:
    used = ["c1"] if citation_ids is None else citation_ids
    return ReportAst.model_validate(
        {
            "report_version": 1,
            "title": "Report",
            "sections": [
                {
                    "section_id": "s1",
                    "heading": "Finding",
                    "level": 2,
                    "blocks": [
                        {
                            "type": "paragraph",
                            "segments": [
                                {
                                    "type": "claim",
                                    "claim_id": "cl1",
                                    "text": "A finding.",
                                    "citation_ids": used,
                                }
                            ],
                        }
                    ],
                }
            ],
            "citations": [{"citation_id": "c1", "evidence_id": evidence_id}],
        }
    )


def test_citation_validator_inline_integrity_and_missing_reference(make_container) -> None:
    container, _ = make_container({})
    task_id, state = task(container, "citation-inline")
    data = {"value": 7}
    state.evidence_pool = [
        Evidence(
            evidence_id="ev1", hypothesis_id="h1", summary="source", excerpt="7",
            source_type="sql", stance="support", relevance=1, data=data,
            content_hash=content_hash(data),
        )
    ]
    validator = CitationValidator(container.session_factory, container.workspace_service)
    valid = validator.validate(
        ast("ev1"), state, tenant_id="tenant_test", user_id="user_test", task_id=task_id
    )
    assert valid.valid

    missing = validator.validate(
        ast("ev1", []), state,
        tenant_id="tenant_test", user_id="user_test", task_id=task_id,
    )
    assert {item.code for item in missing.issues} == {"missing_citation", "unused_citation"}
    assert not missing.valid

    state.evidence_pool[0].content_hash = "0" * 64
    mismatch = validator.validate(
        ast("ev1"), state, tenant_id="tenant_test", user_id="user_test", task_id=task_id
    )
    assert "content_hash_mismatch" in {item.code for item in mismatch.issues}


def test_citation_validator_reports_structure_and_evidence_errors(make_container) -> None:
    container, _ = make_container({})
    task_id, state = task(container, "citation-errors")
    validator = CitationValidator(container.session_factory, container.workspace_service)
    invalid = validator.validate(
        {
            "report_version": 1,
            "title": "bad",
            "sections": [],
            "citations": [],
        },
        state,
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=task_id,
    )
    assert invalid.issues[0].code == "invalid_report_structure"
    missing = validator.validate(
        ast("not_present"), state,
        tenant_id="tenant_test", user_id="user_test", task_id=task_id,
    )
    assert missing.issues[0].code == "evidence_not_found"


def test_workspace_evidence_is_task_scoped(make_container) -> None:
    container, _ = make_container({})
    task_id, state = task(container, "citation-workspace")
    other_id, _ = task(container, "citation-other")
    payload = b"workspace evidence"
    item = container.workspace_service.publish_bytes(
        tenant_id="tenant_test", task_id=task_id, path="files/result.txt", data=payload,
        mime_type="text/plain", operation_key="7" * 64,
    )
    state.evidence_pool = [
        Evidence(
            evidence_id="ev_ws", hypothesis_id="h1", summary="workspace", excerpt="result",
            source_type="sql", stance="support", relevance=1,
            workspace_path=item.workspace_path, content_hash=item.content_hash,
        )
    ]
    validator = CitationValidator(container.session_factory, container.workspace_service)
    assert validator.validate(
        ast("ev_ws"), state, tenant_id="tenant_test", user_id="user_test", task_id=task_id
    ).valid
    denied = validator.validate(
        ast("ev_ws"), state, tenant_id="tenant_test", user_id="user_test", task_id=other_id
    )
    assert "source_not_found" in {issue.code for issue in denied.issues}


def test_compute_image_requires_manifest_lineage_citations(make_container) -> None:
    container, _ = make_container({})
    task_id, state = task(container, "citation-image")
    data = {"rows": [[1]]}
    evidence = Evidence(
        evidence_id="ev_input", hypothesis_id="h1", summary="input", excerpt="1",
        source_type="sql", stance="support", relevance=1, data=data,
        content_hash=content_hash(data),
    )
    state.evidence_pool = [evidence]
    image = b"not-a-real-png-but-integrity-valid"
    output = container.workspace_service.publish_bytes(
        tenant_id="tenant_test", task_id=task_id,
        path="compute/tool1/outputs/chart.png", data=image, mime_type="image/png",
        operation_key="8" * 64, producer_tool_call_id="tool1",
    )
    manifest = {
        "tool_call_id": "tool1",
        "analysis_id": "analysis1",
        "input_evidence": [
            {"evidence_id": evidence.evidence_id, "content_hash": evidence.content_hash}
        ],
        "input_datasets": [],
        "runtime": {},
        "outputs": [
            {"workspace_path": output.workspace_path, "content_hash": output.content_hash}
        ],
    }
    container.workspace_service.publish_bytes(
        tenant_id="tenant_test", task_id=task_id, path="compute/tool1/manifest.json",
        data=canonical_json(manifest), mime_type="application/json",
        operation_key="9" * 64, producer_tool_call_id="tool1",
    )
    image_report = ReportAst.model_validate(
        {
            "report_version": 1,
            "title": "Chart",
            "sections": [
                {
                    "section_id": "s1", "heading": "Chart", "level": 2,
                    "blocks": [
                        {
                            "type": "image", "workspace_path": output.workspace_path,
                            "content_hash": sha256_hex(image), "alt_text": "chart",
                            "citation_ids": ["c1"],
                        }
                    ],
                }
            ],
            "citations": [{"citation_id": "c1", "evidence_id": "ev_input"}],
        }
    )
    validator = CitationValidator(container.session_factory, container.workspace_service)
    assert validator.validate(
        image_report, state,
        tenant_id="tenant_test", user_id="user_test", task_id=task_id,
    ).valid
    broken = image_report.model_copy(deep=True)
    broken.sections[0].blocks[0].citation_ids = []
    result = validator.validate(
        broken, state, tenant_id="tenant_test", user_id="user_test", task_id=task_id
    )
    assert "missing_citation" in {issue.code for issue in result.issues}
