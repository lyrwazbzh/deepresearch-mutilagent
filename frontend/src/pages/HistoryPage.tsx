import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { keys } from "../lib/api/queryKeys";
import type { TaskStatus } from "../lib/api/types";
import { useAuth } from "../auth/AuthProvider";
import { Card, EmptyState, ErrorNotice, PageHeader, StatusBadge, formatDate } from "../components/common/Common";

export function HistoryPage() {
  const { client } = useAuth();
  const [status, setStatus] = useState<TaskStatus | "all">("all");
  const result = useQuery({ queryKey: keys.tasks(), queryFn: () => client.tasks() });
  const items = (result.data?.items ?? []).filter((item) => status === "all" || item.task_status === status);
  return <>
    <PageHeader eyebrow="ARCHIVE" title="研究历史" description="服务器投影是这里唯一的任务状态来源。" actions={<Link className="button primary" to="/workbench">＋ 新建研究</Link>} />
    <Card>
      <div className="toolbar"><label>状态筛选<select value={status} onChange={(e) => setStatus(e.target.value as TaskStatus | "all")}><option value="all">全部状态</option>{["pending", "running", "paused", "completed", "failed", "cancelled"].map((item) => <option key={item}>{item}</option>)}</select></label><button className="button quiet" onClick={() => void result.refetch()}>刷新</button></div>
      {result.error && <ErrorNotice error={result.error} />}
      {!result.isLoading && !items.length ? <EmptyState title="没有匹配的任务" description="调整状态筛选，或开始新的研究。" /> : <div className="list-table history-list">
        {items.map((task) => <Link className="list-row" key={task.task_id} to={`/threads/${task.thread_id}/tasks/${task.task_id}`}><span className="list-primary"><strong>{task.task_id}</strong><small>{task.thread_id}</small></span><span>{task.phase}</span><StatusBadge status={task.task_status} /><time>{formatDate(task.updated_at)}</time><span>›</span></Link>)}
      </div>}
    </Card>
  </>;
}
