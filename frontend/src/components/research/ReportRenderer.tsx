import { useEffect, useMemo, useState } from "react";
import type { ReportAst, ReportBlock, ReportSegment } from "../../lib/api/types";
import { useAuth } from "../../auth/AuthProvider";

function CitationButton({ ids, citations, onCitation }: { ids: string[]; citations: Map<string, string>; onCitation: (evidenceId: string) => void }) {
  if (!ids.length) return null;
  return <>{ids.map((id) => {
    const evidenceId = citations.get(id);
    return <button key={id} className="citation" disabled={!evidenceId} onClick={() => evidenceId && onCitation(evidenceId)} title={evidenceId ? `查看证据 ${evidenceId}` : "引用未找到"}>[{id}]</button>;
  })}</>;
}

function Segments({ items, citations, onCitation }: { items: ReportSegment[]; citations: Map<string, string>; onCitation: (evidenceId: string) => void }) {
  return <>{items.map((segment, index) => segment.type === "text"
    ? <span key={index}>{segment.text}</span>
    : <span key={segment.claim_id} data-claim-id={segment.claim_id}>{segment.text}<CitationButton ids={segment.citation_ids} citations={citations} onCitation={onCitation} /></span>)}</>;
}

function ReportImage({ taskId, block, citations, onCitation }: { taskId: string; block: Extract<ReportBlock, { type: "image" }>; citations: Map<string, string>; onCitation: (evidenceId: string) => void }) {
  const { client } = useAuth();
  const [src, setSrc] = useState<string>();
  useEffect(() => {
    let url: string | undefined;
    client.workspaceBlob(taskId, block.workspace_path).then((blob) => {
      if (!blob.type.startsWith("image/")) return;
      url = URL.createObjectURL(blob); setSrc(url);
    }).catch(() => undefined);
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [block.workspace_path, client, taskId]);
  return <figure className="report-image">{src ? <img src={src} alt={block.alt_text} /> : <div className="image-placeholder">正在加载图像…</div>}<figcaption>{block.caption ?? block.alt_text} <CitationButton ids={block.citation_ids} citations={citations} onCitation={onCitation} /></figcaption></figure>;
}

export function ReportRenderer({ taskId, report, onCitation }: { taskId: string; report: ReportAst; onCitation: (evidenceId: string) => void }) {
  const citations = useMemo(() => new Map(report.citations.map((item) => [item.citation_id, item.evidence_id])), [report.citations]);
  const block = (item: ReportBlock, index: number) => {
    if (item.type === "paragraph") return <p key={index}><Segments items={item.segments} citations={citations} onCitation={onCitation} /></p>;
    if (item.type === "list") {
      const Tag = item.ordered ? "ol" : "ul";
      return <Tag key={index}>{item.items.map((entry, itemIndex) => <li key={itemIndex}><Segments items={entry.segments} citations={citations} onCitation={onCitation} /></li>)}</Tag>;
    }
    if (item.type === "table") return <div className="report-table-wrap" key={index}>{item.caption && <p className="table-caption">{item.caption}</p>}<table><thead><tr>{item.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{item.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table><CitationButton ids={item.citation_ids} citations={citations} onCitation={onCitation} /></div>;
    return <ReportImage key={index} taskId={taskId} block={item} citations={citations} onCitation={onCitation} />;
  };
  return <article className="report-document"><header><span>REPORT · VERSION {report.report_version}</span><h2>{report.title}</h2></header>{report.sections.map((section) => <section key={section.section_id}>{section.level <= 2 ? <h3>{section.heading}</h3> : <h4>{section.heading}</h4>}{section.blocks.map(block)}</section>)}</article>;
}
