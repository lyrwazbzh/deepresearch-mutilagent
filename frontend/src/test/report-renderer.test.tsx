import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReportAst } from "../lib/api/types";
import { ReportRenderer } from "../components/research/ReportRenderer";

const report: ReportAst = {
  report_version: 2,
  title: "可验证的研究报告",
  sections: [{
    section_id: "summary",
    heading: "关键结论",
    level: 1,
    blocks: [
      { type: "paragraph", segments: [{ type: "claim", claim_id: "claim_1", text: "结论来自证据。", citation_ids: ["c1"] }] },
      { type: "table", columns: ["指标", "值"], rows: [["覆盖率", "80%"]], citation_ids: ["c1"] },
      { type: "list", ordered: true, items: [{ segments: [{ type: "text", text: "保持结构化渲染" }] }] },
    ],
  }],
  citations: [{ citation_id: "c1", evidence_id: "evidence_1", locator: "p.1" }],
};

describe("ReportRenderer", () => {
  it("renders ReportAst structurally and resolves citations to evidence", () => {
    const open = vi.fn();
    render(<ReportRenderer taskId="task_1" report={report} onCitation={open} />);
    expect(screen.getByRole("heading", { name: "可验证的研究报告" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("覆盖率");
    fireEvent.click(screen.getAllByRole("button", { name: "[c1]" })[0]!);
    expect(open).toHaveBeenCalledWith("evidence_1");
  });

  it("renders text as text instead of executable HTML", () => {
    const unsafe: ReportAst = { ...report, title: '<img src=x onerror="alert(1)">' };
    const { container } = render(<ReportRenderer taskId="task_1" report={unsafe} onCitation={vi.fn()} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
  });
});
