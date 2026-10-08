import type { PropsWithChildren, ReactNode } from "react";
import { ApiProblem } from "../../lib/api/client";

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return <header className="page-header">
    <div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1>{description && <p>{description}</p>}</div>
    {actions && <div className="header-actions">{actions}</div>}
  </header>;
}

export function Card({ children, className = "" }: PropsWithChildren<{ className?: string }>) {
  return <section className={`card ${className}`}>{children}</section>;
}

export function EmptyState({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-orbit">✦</span><h3>{title}</h3><p>{description}</p>{action}</div>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  const text = error instanceof ApiProblem ? `${error.problem.title}：${error.problem.detail}` : error instanceof Error ? error.message : "发生未知错误";
  return <div className="notice error" role="alert">{text}</div>;
}

export function StatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase().replace(/[^a-z]/g, "-");
  return <span className={`status-badge status-${normalized}`}>{status}</span>;
}

export function formatDate(value?: string) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}
