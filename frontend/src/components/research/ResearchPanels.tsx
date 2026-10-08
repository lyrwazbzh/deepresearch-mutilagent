import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { keys } from "../../lib/api/queryKeys";
import type { Evidence, GraphResult, ReportAst, ReportReference, WorkspaceFile } from "../../lib/api/types";
import { useAuth } from "../../auth/AuthProvider";
import { EmptyState, ErrorNotice, StatusBadge, formatBytes, formatDate } from "../common/Common";
import { ReportRenderer } from "./ReportRenderer";

export function EvidencePanel({ taskId, onOpen }: { taskId: string; onOpen: (item: Evidence) => void }) {
  const { client } = useAuth();
  const [filter, setFilter] = useState("all");
  const result = useQuery({ queryKey: keys.evidence(taskId), queryFn: () => client.evidence(taskId) });
  const items = (result.data?.items ?? []).filter((item) => filter === "all" || item.source_type === filter);
  if (result.error) return <ErrorNotice error={result.error} />;
  return <div><div className="panel-toolbar"><div className="segmented">{["all", "web", "rag", "sql", "compute"].map((value) => <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{value}</button>)}</div><span>{items.length} 条证据</span></div>
    {!items.length ? <EmptyState title="暂无证据" description="研究运行后，证据池会随服务端事件自动刷新。" /> : <div className="evidence-grid">{items.map((item) => <button className="evidence-card" key={item.evidence_id} onClick={() => onOpen(item)}><div><StatusBadge status={item.source_type} /><span className={`stance ${item.stance}`}>{item.stance}</span></div><h4>{item.summary}</h4><p>{item.excerpt}</p><footer><span>相关度 {Math.round(item.relevance * 100)}%</span><span>{item.evidence_id}</span></footer></button>)}</div>}
  </div>;
}

export function EvidenceDrawer({ taskId, evidenceId, fallback, onClose }: { taskId: string; evidenceId?: string; fallback?: Evidence; onClose: () => void }) {
  const { client } = useAuth();
  const result = useQuery({ queryKey: evidenceId ? keys.evidenceItem(taskId, evidenceId) : ["none"], queryFn: () => client.evidenceItem(taskId, evidenceId!), enabled: Boolean(evidenceId) });
  const item = result.data ?? fallback;
  if (!evidenceId && !fallback) return null;
  const safeSource = item?.source_uri && /^https?:\/\//i.test(item.source_uri) ? item.source_uri : null;
  const downloadStored = async () => {
    if (!item) return;
    const blob = item.workspace_path ? await client.workspaceBlob(taskId, item.workspace_path) : item.doc_id ? await client.documentBlob(item.doc_id) : null;
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = item.workspace_path?.split("/").pop() ?? `${item.doc_id}.bin`; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
  };
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="drawer" onMouseDown={(e) => e.stopPropagation()} aria-label="证据详情"><div className="drawer-header"><div><span className="eyebrow">EVIDENCE</span><h3>证据详情</h3></div><button className="icon-button" onClick={onClose} aria-label="关闭">×</button></div>{result.error && <ErrorNotice error={result.error} />}{!item ? <span className="spinner" /> : <div className="detail-stack"><div className="meta-row"><StatusBadge status={item.source_type} /><span>{item.stance}</span><span>相关度 {Math.round(item.relevance * 100)}%</span></div><h4>{item.summary}</h4><blockquote>{item.excerpt}</blockquote><dl><dt>Evidence ID</dt><dd>{item.evidence_id}</dd><dt>Hypothesis</dt><dd>{item.hypothesis_id}</dd><dt>Content hash</dt><dd className="mono">{item.content_hash}</dd>{item.doc_id && <><dt>Document</dt><dd>{item.doc_id} / {item.chunk_id}</dd></>}{item.workspace_path && <><dt>Workspace</dt><dd>{item.workspace_path}</dd></>}{item.retrieved_at && <><dt>检索时间</dt><dd>{formatDate(item.retrieved_at)}</dd></>}</dl>{item.data && <pre>{JSON.stringify(item.data, null, 2)}</pre>}<div className="row-actions">{safeSource && <a className="button secondary" href={safeSource} target="_blank" rel="noreferrer">打开 Web 来源 ↗</a>}{(item.doc_id || item.workspace_path) && <button className="button secondary" onClick={() => void downloadStored()}>下载存储来源</button>}</div></div>}</aside></div>;
}

export function KnowledgeGraphPanel({ taskId, onEvidence }: { taskId: string; onEvidence: (id: string) => void }) {
  const { client } = useAuth();
  const [op, setOp] = useState<"search" | "neighbors" | "path">("search");
  const [query, setQuery] = useState("");
  const [target, setTarget] = useState("");
  const [result, setResult] = useState<GraphResult>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(undefined);
    const body = op === "search" ? { op, query, limit: 30 } : op === "neighbors" ? { op, entity: query, limit: 30 } : { op, entity: query, target_entity: target, max_hops: 3, limit: 30 };
    try { setResult(await client.graph(taskId, body)); } catch (reason) { setError(reason); } finally { setBusy(false); }
  };
  return <div className="kg-panel"><form className="kg-query" onSubmit={(e) => void submit(e)}><select value={op} onChange={(e) => setOp(e.target.value as typeof op)}><option value="search">全文搜索</option><option value="neighbors">邻居查询</option><option value="path">路径查询</option></select><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={op === "search" ? "搜索实体与主张" : "起始实体"} required />{op === "path" && <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="目标实体" required />}<button className="button primary" disabled={busy}>{busy ? "查询中…" : "查询图谱"}</button></form>{error !== undefined && <ErrorNotice error={error} />}{!result ? <EmptyState title="查询任务知识图谱" description="按关键词、实体邻居或实体路径探索当前任务的可追溯知识。" /> : <><div className="kg-summary"><StatusBadge status={result.graph_status} /><span>{result.entities.length} 个实体</span><span>{result.claims.length} 条主张</span>{result.truncated && <span>结果已截断</span>}</div><div className="entity-cloud">{result.entities.map((entity) => <span key={entity.entity_id}><small>{entity.entity_type}</small>{entity.canonical_name}</span>)}</div><div className="claim-list">{result.claims.map((claim) => <article key={claim.claim_id}><strong>{claim.predicate}</strong><p>{claim.value ?? `${claim.subject_entity_id ?? "?"} → ${claim.object_entity_id ?? "?"}`}</p><footer>{claim.provenance.map((source) => <button className="citation" key={`${claim.claim_id}-${source.evidence_id}`} onClick={() => onEvidence(source.evidence_id)}>{source.evidence_id}</button>)}</footer></article>)}</div></>}</div>;
}

function WorkspacePreview({ taskId, file }: { taskId: string; file: WorkspaceFile }) {
  const { client } = useAuth();
  const [url, setUrl] = useState<string>();
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    let objectUrl: string | undefined;
    client.workspaceBlob(taskId, file.workspace_path).then((blob) => {
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch(setError);
    return () => { if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [client, file.workspace_path, taskId]);
  if (error) return <ErrorNotice error={error} />;
  if (!url) return <div className="preview-loading"><span className="spinner" />加载文件…</div>;
  if (file.mime_type.startsWith("image/")) return <img className="workspace-image" src={url} alt={file.workspace_path} />;
  if (file.mime_type === "application/pdf") return <iframe className="workspace-frame" src={url} title={file.workspace_path} />;
  return <a className="button secondary" href={url} download={file.workspace_path.split("/").pop()}>下载 {file.workspace_path.split("/").pop()}</a>;
}

export function WorkspacePanel({ taskId }: { taskId: string }) {
  const { client } = useAuth();
  const [selected, setSelected] = useState<WorkspaceFile>();
  const result = useQuery({ queryKey: keys.workspace(taskId), queryFn: () => client.workspace(taskId) });
  if (result.error) return <ErrorNotice error={result.error} />;
  const files = result.data?.files ?? [];
  return <div className="workspace-layout"><div className="workspace-files">{!files.length ? <EmptyState title="工作区尚为空" description="数据文件、计算产物和报告素材会出现在这里。" /> : files.map((file) => <button key={file.workspace_path} className={selected?.workspace_path === file.workspace_path ? "file-row active" : "file-row"} onClick={() => setSelected(file)}><span>{file.mime_type.startsWith("image/") ? "▧" : "▱"}</span><span><strong>{file.workspace_path}</strong><small>{file.mime_type} · {formatBytes(file.size)}</small></span></button>)}</div><div className="workspace-preview">{selected ? <><div className="preview-title"><strong>{selected.workspace_path}</strong><StatusBadge status={selected.status} /></div><WorkspacePreview taskId={taskId} file={selected} /></> : <EmptyState title="选择一个产物" description="预览图像、PDF，或下载其他文件。" />}</div></div>;
}

export function ReportPanel({ taskId, reference, final, onEvidence }: { taskId: string; reference?: ReportReference; final: boolean; onEvidence: (id: string) => void }) {
  const { client } = useAuth();
  const result = useQuery({ queryKey: keys.report(taskId), queryFn: async () => {
    if (final) return (await client.report(taskId)).report;
    if (!reference) throw new Error("Report reference is missing.");
    const text = await (await client.workspaceBlob(taskId, reference.workspace_path)).text();
    return JSON.parse(text) as ReportAst;
  }, enabled: Boolean(reference) });
  if (!reference) return <EmptyState title="报告尚未生成" description="研究形成草稿或通过引用校验后，ReportAst 会在此渲染。" />;
  if (result.error) return <ErrorNotice error={result.error} />;
  if (!result.data) return <div className="preview-loading"><span className="spinner" />加载报告…</div>;
  return <><div className="report-kind"><StatusBadge status={final ? "final" : "draft"} />{final ? "权威最终报告" : "服务端草稿"}</div><ReportRenderer taskId={taskId} report={result.data} onCitation={onEvidence} /></>;
}
