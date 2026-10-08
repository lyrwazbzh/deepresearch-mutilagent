import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { idempotencyKey } from "../lib/api/client";
import { keys } from "../lib/api/queryKeys";
import type { DocumentItem } from "../lib/api/types";
import { useAuth } from "../auth/AuthProvider";
import { Card, EmptyState, ErrorNotice, PageHeader, StatusBadge, formatBytes, formatDate } from "../components/common/Common";

function DocumentActions({ item }: { item: DocumentItem }) {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState<string>();
  const ingestion = useQuery({ queryKey: keys.ingestion(item.doc_id), queryFn: () => client.ingestion(item.doc_id) });
  const run = async (action: "retry" | "rebuild" | "delete") => {
    if (action === "delete" && !window.confirm(`确认删除文档“${item.name}”？`)) return;
    setBusy(action); setError(undefined);
    try {
      if (action === "retry") await client.retryIngestion(item.doc_id);
      if (action === "rebuild") await client.rebuildDocument(item.doc_id);
      if (action === "delete") await client.deleteDocument(item.doc_id);
      await queryClient.invalidateQueries({ queryKey: keys.documents });
      await queryClient.invalidateQueries({ queryKey: keys.ingestion(item.doc_id) });
    } catch (cause) { setError(cause); } finally { setBusy(undefined); }
  };
  const download = async () => {
    setBusy("download"); setError(undefined);
    try {
      const blob = await client.documentBlob(item.doc_id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = item.name; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    } catch (cause) { setError(cause); } finally { setBusy(undefined); }
  };
  return <div className="document-detail"><div className="ingestion-line"><span>摄取阶段</span><strong>{ingestion.data?.ingestion.stage ?? "—"}</strong><StatusBadge status={ingestion.data?.ingestion.status ?? item.status} /><small>尝试 {ingestion.data?.ingestion.attempt_count ?? 0} 次</small></div><div className="row-actions"><button className="button secondary" disabled={Boolean(busy)} onClick={() => void download()}>下载原文</button>{item.status === "failed" && <button className="button quiet" disabled={Boolean(busy)} onClick={() => void run("retry")}>重试摄取</button>}<button className="button quiet" disabled={Boolean(busy)} onClick={() => void run("rebuild")}>重新构建</button><button className="button danger" disabled={Boolean(busy)} onClick={() => void run("delete")}>删除</button></div>{error !== undefined && <ErrorNotice error={error} />}</div>;
}

export function DocumentsPage() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<number>();
  const [error, setError] = useState<unknown>();
  const [expanded, setExpanded] = useState<string>();
  const result = useQuery({ queryKey: keys.documents, queryFn: () => client.documents(), refetchInterval: (query) => query.state.data?.items.some((item) => item.status === "pending") ? 4_000 : false });
  const upload = async (file: File) => {
    const form = new FormData(); form.append("file", file);
    setProgress(0); setError(undefined);
    try {
      await client.upload("/api/v1/documents", form, idempotencyKey(), setProgress);
      setProgress(undefined); await queryClient.invalidateQueries({ queryKey: keys.documents });
    } catch (cause) { setError(cause); setProgress(undefined); }
  };
  return <><PageHeader eyebrow="KNOWLEDGE BASE" title="知识文档" description="上传、跟踪摄取状态，并在失败时按正式生命周期重试或重建。" actions={<label className="button primary">＋ 上传文档<input hidden type="file" accept=".txt,.md,.html,.pdf,.docx" onChange={(e) => { const file = e.target.files?.[0]; if (file) void upload(file); e.target.value = ""; }} /></label>} />
    {progress !== undefined && <div className="upload-progress"><div style={{ width: `${progress}%` }} /><span>正在上传 {progress}%</span></div>}{error !== undefined && <ErrorNotice error={error} />}
    <Card>{result.error && <ErrorNotice error={result.error} />}{!result.isLoading && !result.data?.items.length ? <EmptyState title="知识库为空" description="上传文档后，摄取任务会把内容发布到检索索引。" /> : <div className="document-list">{result.data?.items.map((item) => <article key={item.doc_id} className="document-item"><button className="document-row" onClick={() => setExpanded(expanded === item.doc_id ? undefined : item.doc_id)}><span className="doc-icon">▤</span><span className="list-primary"><strong>{item.name}</strong><small>{item.media_type} · {formatBytes(item.size_bytes)} · {formatDate(item.updated_at)}</small></span><StatusBadge status={item.status} /><span>{expanded === item.doc_id ? "⌃" : "⌄"}</span></button>{expanded === item.doc_id && <DocumentActions item={item} />}</article>)}</div>}</Card>
  </>;
}
