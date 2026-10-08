import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { idempotencyKey } from "../lib/api/client";
import { keys } from "../lib/api/queryKeys";
import type { Evidence, PendingWrite, TaskProjection } from "../lib/api/types";
import { useAuth } from "../auth/AuthProvider";
import { Card, ErrorNotice, PageHeader, StatusBadge, formatDate } from "../components/common/Common";
import { EvidenceDrawer, EvidencePanel, KnowledgeGraphPanel, ReportPanel, WorkspacePanel } from "../components/research/ResearchPanels";
import { useTaskEvents } from "../hooks/useTaskEvents";

const phases = ["routing", "planning", "retrieving", "analyzing", "reviewing", "writing", "completed"];
const phaseLabels: Record<string, string> = { routing: "路由", planning: "规划", retrieving: "检索", analyzing: "分析", reviewing: "审阅", writing: "撰写", completed: "完成" };

function TaskComposer({ threadId, compact = false }: { threadId?: string; compact?: boolean }) {
  const { client } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [maxDepth, setMaxDepth] = useState(3);
  const [maxRevision, setMaxRevision] = useState(2);
  const [selectedSources, setSelectedSources] = useState<string[]>([]);
  const actionKey = useRef<string | undefined>(undefined);
  const sources = useQuery({ queryKey: keys.dataSources, queryFn: () => client.dataSources() });
  const mutation = useMutation({
    mutationFn: async () => {
      actionKey.current ??= idempotencyKey();
      return client.createTask({ query, ...(threadId ? { thread_id: threadId } : {}), options: { max_depth: maxDepth, max_revision_rounds: maxRevision, datasource_ids: selectedSources } }, actionKey.current);
    },
    onSuccess: (task) => {
      actionKey.current = undefined;
      setQuery("");
      void queryClient.invalidateQueries({ queryKey: keys.threads });
      void queryClient.invalidateQueries({ queryKey: keys.tasks(task.thread_id) });
      void navigate(`/threads/${task.thread_id}/tasks/${task.task_id}`);
    },
  });
  const submit = (event: FormEvent) => { event.preventDefault(); if (query.trim()) mutation.mutate(); };
  return <form className={compact ? "composer compact" : "composer"} onSubmit={submit}>
    <div className="composer-main"><textarea value={query} onChange={(e) => { setQuery(e.target.value); actionKey.current = undefined; }} placeholder={threadId ? "继续追问或提出新的研究方向…" : "输入一个值得深入研究的问题…"} rows={compact ? 2 : 4} required /><button className="button primary send" disabled={mutation.isPending || !query.trim()}>{mutation.isPending ? "提交中…" : "开始研究 →"}</button></div>
    <div className="composer-footer"><button type="button" className="text-button" onClick={() => setAdvanced(!advanced)}>研究参数 {advanced ? "▴" : "▾"}</button><span>请求会携带稳定幂等键</span></div>
    {advanced && <div className="advanced-options"><label>最大研究深度<input type="number" min="0" max="5" value={maxDepth} onChange={(e) => setMaxDepth(Number(e.target.value))} /></label><label>最大修订轮次<input type="number" min="0" max="5" value={maxRevision} onChange={(e) => setMaxRevision(Number(e.target.value))} /></label><fieldset><legend>可用数据源</legend>{sources.data?.items.filter((item) => item.enabled).map((source) => <label className="check" key={source.datasource_id}><input type="checkbox" checked={selectedSources.includes(source.datasource_id)} onChange={(e) => setSelectedSources((current) => e.target.checked ? [...current, source.datasource_id] : current.filter((id) => id !== source.datasource_id))} />{source.name}</label>)}{!sources.data?.items.length && <small>当前没有可用数据源</small>}</fieldset></div>}
    {mutation.error !== null && <ErrorNotice error={mutation.error} />}
  </form>;
}

function TaskProgress({ task }: { task: TaskProjection }) {
  const index = Math.max(0, phases.indexOf(task.phase));
  return <div className="phase-track" aria-label={`当前阶段：${phaseLabels[task.phase] ?? task.phase}`}>{phases.map((phase, phaseIndex) => <div key={phase} className={phaseIndex < index ? "phase done" : phaseIndex === index ? "phase current" : "phase"}><i>{phaseIndex < index ? "✓" : phaseIndex + 1}</i><span>{phaseLabels[phase]}</span></div>)}</div>;
}

function TaskControls({ task }: { task: TaskProjection }) {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<string>();
  const [error, setError] = useState<unknown>();
  const [showReplan, setShowReplan] = useState(false);
  const [instruction, setInstruction] = useState("");
  useEffect(() => { setPending(undefined); }, [task.task_status, task.updated_at]);
  const run = async (command: "pause" | "resume" | "cancel") => {
    setPending(command); setError(undefined);
    try {
      const projection = await client.taskCommand(task.task_id, command, idempotencyKey());
      queryClient.setQueryData(keys.task(task.task_id), projection);
    } catch (reason) { setPending(undefined); setError(reason); }
  };
  const replan = async (event: FormEvent) => {
    event.preventDefault();
    setPending("replan"); setError(undefined);
    try {
      const projection = await client.replan(task.task_id, instruction, idempotencyKey());
      queryClient.setQueryData(keys.task(task.task_id), projection);
      setInstruction(""); setShowReplan(false);
    } catch (reason) { setPending(undefined); setError(reason); }
  };
  const canPause = task.task_status === "running";
  const canResume = task.task_status === "paused" && !task.pending_write;
  const canCancel = ["pending", "running", "pausing", "paused"].includes(task.task_status);
  return <div className="task-control-wrap"><div className="task-controls">{!["completed", "cancelled"].includes(task.task_status) && <button className="button quiet" disabled={Boolean(pending)} onClick={() => setShowReplan(!showReplan)}>调整计划</button>}{canPause && <button className="button secondary" disabled={Boolean(pending)} onClick={() => void run("pause")}>Ⅱ {pending === "pause" ? "正在请求…" : "暂停"}</button>}{canResume && <button className="button secondary" disabled={Boolean(pending)} onClick={() => void run("resume")}>▶ {pending === "resume" ? "正在请求…" : "继续"}</button>}{canCancel && <button className="button danger" disabled={Boolean(pending)} onClick={() => void run("cancel")}>× {pending === "cancel" ? "正在请求…" : "取消"}</button>}</div>{showReplan && <form className="replan-form" onSubmit={(event) => void replan(event)}><input value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="说明希望如何调整研究计划" required /><button className="button secondary" disabled={Boolean(pending)}>{pending === "replan" ? "提交中…" : "提交调整"}</button></form>}{error !== undefined && <ErrorNotice error={error} />}</div>;
}

function PendingWriteCard({ task, value }: { task: TaskProjection; value: PendingWrite }) {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<unknown>();
  const decide = async (action: "approve" | "reject") => {
    setBusy(action); setError(undefined);
    try {
      const next = action === "approve"
        ? await client.approveWrite(task.task_id, value.write_id, value.plan_hash, comment, idempotencyKey())
        : await client.rejectWrite(task.task_id, value.write_id, reason, idempotencyKey());
      queryClient.setQueryData(keys.pendingWrite(task.task_id, value.write_id), next);
      await queryClient.invalidateQueries({ queryKey: keys.task(task.task_id) });
    } catch (cause) { setError(cause); } finally { setBusy(undefined); }
  };
  return <Card className="write-card"><div className="write-warning"><span>!</span><div><small>HUMAN APPROVAL REQUIRED</small><h3>数据库写入等待你的决定</h3></div><StatusBadge status={value.status} /></div><div className="write-grid"><dl><dt>数据源</dt><dd>{value.preview.datasource}</dd><dt>操作</dt><dd>{value.preview.operation.toUpperCase()}</dd><dt>目标表</dt><dd>{value.preview.table}</dd><dt>原因</dt><dd>{value.preview.reason}</dd></dl><div><h4>预览</h4><pre>{JSON.stringify({ filters: value.preview.filters, values: value.preview.values, current_version: value.preview.current_version, expected_value: value.preview.expected_value }, null, 2)}</pre></div></div>{value.status === "pending" && <div className="decision-area"><label>批准备注（可选）<input value={comment} onChange={(e) => setComment(e.target.value)} /></label><button className="button primary" disabled={Boolean(busy)} onClick={() => void decide("approve")}>{busy === "approve" ? "提交中…" : "批准写入"}</button><label>拒绝原因（可选）<input value={reason} onChange={(e) => setReason(e.target.value)} /></label><button className="button danger" disabled={Boolean(busy)} onClick={() => void decide("reject")}>{busy === "reject" ? "提交中…" : "拒绝"}</button></div>}{error !== undefined && <ErrorNotice error={error} />}</Card>;
}

function PendingWriteGate({ task }: { task: TaskProjection }) {
  const { client } = useAuth();
  const result = useQuery({ queryKey: task.pending_write ? keys.pendingWrite(task.task_id, task.pending_write) : ["none"], queryFn: () => client.pendingWrite(task.task_id, task.pending_write!), enabled: Boolean(task.pending_write), refetchInterval: 5_000 });
  if (!task.pending_write) return null;
  if (result.error) return <ErrorNotice error={result.error} />;
  return result.data ? <PendingWriteCard task={task} value={result.data} /> : <Card><span className="spinner" />加载写入审批…</Card>;
}

function DataFileUpload({ taskId }: { taskId: string }) {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<number>();
  const [error, setError] = useState<unknown>();
  const upload = async (file: File) => {
    const form = new FormData(); form.append("file", file);
    setProgress(0); setError(undefined);
    try {
      await client.upload(`/api/v1/tasks/${encodeURIComponent(taskId)}/data-files`, form, idempotencyKey(), setProgress);
      setProgress(undefined); await queryClient.invalidateQueries({ queryKey: keys.workspace(taskId) });
    } catch (cause) { setError(cause); setProgress(undefined); }
  };
  return <div className="data-upload"><label className="button quiet">＋ 上传任务数据<input type="file" accept=".csv,.json,.jsonl,.parquet,.xlsx" hidden onChange={(e) => { const file = e.target.files?.[0]; if (file) void upload(file); e.target.value = ""; }} /></label>{progress !== undefined && <span>上传 {progress}%</span>}{error !== undefined && <ErrorNotice error={error} />}</div>;
}

export function WorkbenchPage() {
  const { threadId, taskId } = useParams();
  const { client } = useAuth();
  const navigate = useNavigate();
  const [tab, setTab] = useState<"evidence" | "graph" | "workspace" | "report">("evidence");
  const [evidence, setEvidence] = useState<{ id?: string; item?: Evidence }>();
  const taskList = useQuery({ queryKey: keys.tasks(threadId), queryFn: () => client.tasks(threadId), enabled: Boolean(threadId) });
  const messages = useQuery({ queryKey: threadId ? keys.messages(threadId) : ["none"], queryFn: () => client.messages(threadId!), enabled: Boolean(threadId) });
  const task = useQuery({ queryKey: taskId ? keys.task(taskId) : ["none"], queryFn: () => client.task(taskId!), enabled: Boolean(taskId) });
  const connection = useTaskEvents(taskId);

  useEffect(() => {
    if (threadId && !taskId && taskList.data?.items[0]) void navigate(`/threads/${threadId}/tasks/${taskList.data.items[0].task_id}`, { replace: true });
  }, [navigate, taskId, taskList.data, threadId]);
  useEffect(() => { setEvidence(undefined); setTab("evidence"); }, [taskId]);

  const selected = task.data;
  const reportReference = selected?.report.final ?? selected?.report.draft;
  const tabItems = useMemo(() => [["evidence", "搜索结果"], ["graph", "知识图谱"], ["workspace", "可视化"], ["report", "研究报告"]] as const, []);
  if (!threadId) return <><PageHeader eyebrow="NEW RESEARCH" title="研究工作台" description="提出问题，系统会创建对话并启动受预算约束的研究图。" /><div className="new-research-hero"><span className="hero-glyph">✦</span><h2>今天想弄清什么？</h2><p>越具体的问题，越容易得到有边界、有证据、可复核的结论。</p><TaskComposer /></div></>;

  return <div className="workbench-page">
    <PageHeader eyebrow="LIVE RESEARCH" title="研究工作台" description={`Thread · ${threadId}`} actions={<div className="connection-state"><i className={connection === "live" ? "online" : ""} />{connection === "live" ? "事件已连接" : connection === "idle" ? "等待任务" : connection === "resyncing" ? "正在重新协调" : "正在重连"}</div>} />
    <div className="workbench-grid">
      <aside className="conversation-panel card"><div className="section-heading"><div><span className="eyebrow">CONVERSATION</span><h2>研究对话</h2></div></div><div className="task-switcher"><label>当前任务<select value={taskId ?? ""} onChange={(e) => void navigate(`/threads/${threadId}/tasks/${e.target.value}`)}>{taskList.data?.items.map((item) => <option key={item.task_id} value={item.task_id}>{item.task_id.slice(-8)} · {item.task_status}</option>)}</select></label></div><div className="message-list">{messages.data?.items.map((message) => <article className={`message ${message.role}`} key={message.message_id}><span>{message.role === "user" ? "你" : "DR"}</span><div><small>{message.role === "user" ? "研究问题" : "研究助理"} · {formatDate(message.created_at)}</small><p>{message.content}</p></div></article>)}</div><TaskComposer threadId={threadId} compact /></aside>
      <section className="research-panel">
        {task.error !== null && <ErrorNotice error={task.error} />}
        {!selected ? <Card><span className="spinner" />加载任务投影…</Card> : <>
          <Card className="task-overview"><div className="task-title"><div><span className="eyebrow">TASK {selected.task_id.slice(-8).toUpperCase()}</span><h2>{selected.phase === "completed" ? "研究已完成" : `正在${phaseLabels[selected.phase] ?? selected.phase}`}</h2></div><StatusBadge status={selected.task_status} /></div><TaskProgress task={selected} /><div className="overview-footer"><div><span>路由策略</span><strong>{selected.route?.mode ?? "待定"}{selected.route?.target_agent ? ` · ${selected.route.target_agent}` : ""}</strong></div><div><span>更新时间</span><strong>{formatDate(selected.updated_at)}</strong></div><TaskControls task={selected} /></div></Card>
          <PendingWriteGate task={selected} />
          <Card className="research-resources"><div className="resource-tabs" role="tablist">{tabItems.map(([value, label]) => <button role="tab" aria-selected={tab === value} key={value} className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{label}{value === "report" && reportReference && <i />}</button>)}<DataFileUpload taskId={selected.task_id} /></div><div className="resource-content">{tab === "evidence" && <EvidencePanel taskId={selected.task_id} onOpen={(item) => setEvidence({ item })} />}{tab === "graph" && <KnowledgeGraphPanel taskId={selected.task_id} onEvidence={(id) => setEvidence({ id })} />}{tab === "workspace" && <WorkspacePanel taskId={selected.task_id} />}{tab === "report" && <ReportPanel taskId={selected.task_id} {...(reportReference ? { reference: reportReference } : {})} final={Boolean(selected.report.final)} onEvidence={(id) => setEvidence({ id })} />}</div></Card>
        </>}
      </section>
    </div>{selected && evidence && <EvidenceDrawer taskId={selected.task_id} {...(evidence.id ? { evidenceId: evidence.id } : {})} {...(evidence.item ? { fallback: evidence.item } : {})} onClose={() => setEvidence(undefined)} />}
  </div>;
}
