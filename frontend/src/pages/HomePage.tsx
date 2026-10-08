import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { keys } from "../lib/api/queryKeys";
import { useAuth } from "../auth/AuthProvider";
import { Card, EmptyState, PageHeader, StatusBadge, formatDate } from "../components/common/Common";

export function HomePage() {
  const { client, session } = useAuth();
  const tasks = useQuery({ queryKey: keys.tasks(), queryFn: () => client.tasks() });
  const threads = useQuery({ queryKey: keys.threads, queryFn: () => client.threads() });
  const items = tasks.data?.items ?? [];
  const active = items.filter((item) => !["completed", "failed", "cancelled"].includes(item.task_status)).length;
  return <>
    <PageHeader eyebrow="RESEARCH OVERVIEW" title={`你好，${session?.user.email.split("@")[0]}`} description="从一个清晰的问题开始，研究过程与证据会持续同步到这里。" actions={<Link className="button primary" to="/workbench">＋ 新建研究</Link>} />
    <div className="metric-grid">
      <Card><span>研究总数</span><strong>{items.length}</strong><small>跨 {threads.data?.items.length ?? 0} 个对话</small></Card>
      <Card><span>进行中</span><strong>{active}</strong><small>实时事件保持同步</small></Card>
      <Card><span>已完成</span><strong>{items.filter((item) => item.task_status === "completed").length}</strong><small>可查看报告与引用</small></Card>
    </div>
    <Card className="recent-card">
      <div className="section-heading"><div><span className="eyebrow">RECENT</span><h2>最近研究</h2></div><Link to="/history">查看全部 →</Link></div>
      {!items.length ? <EmptyState title="还没有研究任务" description="向工作台提交问题，系统会为你建立研究对话。" action={<Link className="button primary" to="/workbench">开始第一项研究</Link>} /> :
        <div className="list-table">{items.slice(0, 6).map((task) => <Link className="list-row" key={task.task_id} to={`/threads/${task.thread_id}/tasks/${task.task_id}`}>
          <span className="list-primary"><strong>{task.task_id}</strong><small>{task.phase} · {task.route?.mode ?? "等待路由"}</small></span>
          <StatusBadge status={task.task_status} /><time>{formatDate(task.updated_at)}</time><span>›</span>
        </Link>)}</div>}
    </Card>
  </>;
}
