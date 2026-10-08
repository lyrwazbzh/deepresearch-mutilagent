from __future__ import annotations

from src.agent.tools.database_write import SqlCompiler
from src.agent.identity import content_hash
from src.agent.schema.report import ReportAst
from src.agent.schema.state import Evidence
from src.agent.schema.write import WritePlan
from src.agent.reporting.reporting import MarkdownRenderer


def test_write_plan_rejects_unconditional_mutation_and_compiles_parameters() -> None:
    try:
        WritePlan(
            plan_id="p1",
            datasource_id="ds1",
            operation="delete",
            table="public.accounts",
            reason="cleanup",
        )
    except ValueError:
        pass
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unconditional delete was accepted")

    plan = WritePlan(
        plan_id="p2",
        datasource_id="ds1",
        operation="update",
        table="public.accounts",
        values={"status": "active"},
        filters=[{"column": "id", "op": "IN", "value": [1, 2]}],
        preconditions={"version": 3},
        reason="approved correction",
    )
    compiled = SqlCompiler().compile(plan)
    assert compiled.sql == (
        'UPDATE "public"."accounts" SET "status" = %s '
        'WHERE "id" IN (%s, %s) AND "version" = %s'
    )
    assert compiled.parameters == ("active", 1, 2, 3)
    assert "active" not in compiled.sql


def test_markdown_render_is_deterministic() -> None:
    data = {"answer": 42}
    evidence = Evidence(
        evidence_id="ev1",
        hypothesis_id="h1",
        summary="Measured answer",
        excerpt="42",
        source_type="sql",
        stance="support",
        relevance=1,
        data=data,
        content_hash=content_hash(data),
    )
    report = ReportAst.model_validate(
        {
            "report_version": 1,
            "title": "Result",
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
                                    "claim_id": "claim1",
                                    "text": "The answer is 42.",
                                    "citation_ids": ["c1"],
                                }
                            ],
                        }
                    ],
                }
            ],
            "citations": [{"citation_id": "c1", "evidence_id": "ev1"}],
        }
    )
    renderer = MarkdownRenderer()
    assert renderer.render(report, [evidence]) == renderer.render(report, [evidence])
    assert b"[^cite-c1]" in renderer.render(report, [evidence])
