import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { keys } from "../lib/api/queryKeys";
import type { DataSource } from "../lib/api/types";
import { useAuth } from "../auth/AuthProvider";
import { Card, EmptyState, ErrorNotice, PageHeader, StatusBadge } from "../components/common/Common";

function PolicyEditor({ source }: { source: DataSource }) {
  const { client } = useAuth();
  const [schema, setSchema] = useState("public");
  const [table, setTable] = useState("");
  const [readable, setReadable] = useState("");
  const [writable, setWritable] = useState("");
  const [allowInsert, setAllowInsert] = useState(false);
  const [allowUpdate, setAllowUpdate] = useState(false);
  const [allowDelete, setAllowDelete] = useState(false);
  const [maxRows, setMaxRows] = useState(100);
  const [userId, setUserId] = useState("");
  const [access, setAccess] = useState<"read" | "write">("read");
  const [message, setMessage] = useState<string>();
  const [error, setError] = useState<unknown>();
  const savePolicy = async (event: FormEvent) => {
    event.preventDefault(); setError(undefined);
    try {
      await client.replacePolicies(source.datasource_id, [{ schema_name: schema, table_name: table, readable_columns: readable.split(",").map((v) => v.trim()).filter(Boolean), writable_columns: writable.split(",").map((v) => v.trim()).filter(Boolean), allow_insert: allowInsert, allow_update: allowUpdate, allow_delete: allowDelete, max_write_rows: maxRows }]);
      setMessage("策略已由服务端替换");
    } catch (cause) { setError(cause); }
  };
  const grant = async (event: FormEvent) => {
    event.preventDefault(); setError(undefined);
    try { await client.putGrant(source.datasource_id, userId, access); setMessage(`已授予 ${access} 权限`); } catch (cause) { setError(cause); }
  };
  return <div className="admin-editors"><form onSubmit={(e) => void savePolicy(e)}><h4>表级策略（全量替换）</h4><div className="form-grid"><label>Schema<input value={schema} onChange={(e) => setSchema(e.target.value)} required /></label><label>Table<input value={table} onChange={(e) => setTable(e.target.value)} required /></label><label className="span-2">可读列（逗号分隔）<input value={readable} onChange={(e) => setReadable(e.target.value)} /></label><label className="span-2">可写列（逗号分隔）<input value={writable} onChange={(e) => setWritable(e.target.value)} /></label><label>最大写入行数<input type="number" min="0" value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value))} /></label></div><div className="checks"><label><input type="checkbox" checked={allowInsert} onChange={(e) => setAllowInsert(e.target.checked)} /> INSERT</label><label><input type="checkbox" checked={allowUpdate} onChange={(e) => setAllowUpdate(e.target.checked)} /> UPDATE</label><label><input type="checkbox" checked={allowDelete} onChange={(e) => setAllowDelete(e.target.checked)} /> DELETE</label></div><button className="button secondary">保存策略</button></form><form onSubmit={(e) => void grant(e)}><h4>用户授权</h4><label>用户 ID<input value={userId} onChange={(e) => setUserId(e.target.value)} required /></label><label>权限<select value={access} onChange={(e) => setAccess(e.target.value as typeof access)}><option value="read">read</option><option value="write">write</option></select></label><button className="button secondary">保存授权</button></form>{message && <div className="notice success">{message}</div>}{error !== undefined && <ErrorNotice error={error} />}</div>;
}

function ConnectionEditor({ source }: { source: DataSource }) {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [name, setName] = useState(source.name);
  const [host, setHost] = useState(source.host ?? "");
  const [port, setPort] = useState(source.port ?? 5432);
  const [databaseName, setDatabaseName] = useState(source.database_name ?? "");
  const [sslMode, setSslMode] = useState(source.ssl_mode ?? "require");
  const [credential, setCredential] = useState("");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<unknown>();
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(undefined); setSaved(false);
    try {
      await client.patchDataSource(source.datasource_id, { name, host, port, database_name: databaseName, ssl_mode: sslMode, ...(credential ? { credential_ref: credential } : {}) });
      setCredential(""); setSaved(true); await queryClient.invalidateQueries({ queryKey: keys.dataSources });
    } catch (cause) { setError(cause); }
  };
  return <form className="connection-editor" onSubmit={(event) => void submit(event)}><h4>连接信息</h4><div className="form-grid"><label>名称<input value={name} onChange={(event) => setName(event.target.value)} required /></label><label>主机<input value={host} onChange={(event) => setHost(event.target.value)} required /></label><label>端口<input type="number" value={port} onChange={(event) => setPort(Number(event.target.value))} required /></label><label>数据库<input value={databaseName} onChange={(event) => setDatabaseName(event.target.value)} required /></label><label>SSL 模式<input value={sslMode} onChange={(event) => setSslMode(event.target.value)} required /></label><label>替换凭据引用<input type="password" autoComplete="off" value={credential} onChange={(event) => setCredential(event.target.value)} placeholder="留空则不变" /></label></div><button className="button secondary">保存连接</button>{saved && <span className="inline-success">已保存</span>}{error !== undefined && <ErrorNotice error={error} />}</form>;
}

export function DataSourcesPage() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<string>();
  const [error, setError] = useState<unknown>();
  const [form, setForm] = useState({ name: "", host: "", port: 5432, database_name: "", ssl_mode: "require", credential_ref: "" });
  const result = useQuery({ queryKey: keys.dataSources, queryFn: () => client.dataSources() });
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(undefined);
    try { await client.createDataSource(form); setOpen(false); setForm({ name: "", host: "", port: 5432, database_name: "", ssl_mode: "require", credential_ref: "" }); await queryClient.invalidateQueries({ queryKey: keys.dataSources }); } catch (cause) { setError(cause); }
  };
  const toggle = async (source: DataSource) => { try { await client.toggleDataSource(source.datasource_id, !source.enabled); await queryClient.invalidateQueries({ queryKey: keys.dataSources }); } catch (cause) { setError(cause); } };
  return <><PageHeader eyebrow="STRUCTURED DATA" title="数据源" description="管理 SQL 数据源、表级允许列表和用户授权。此页面不会调用额外的连接测试路由。" actions={<button className="button primary" onClick={() => setOpen(!open)}>＋ 新建数据源</button>} />
    {open && <Card><form className="admin-form" onSubmit={(e) => void submit(e)}><h3>连接配置</h3><div className="form-grid"><label>名称<input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></label><label>主机<input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} required /></label><label>端口<input type="number" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} required /></label><label>数据库<input value={form.database_name} onChange={(e) => setForm({ ...form, database_name: e.target.value })} required /></label><label>SSL 模式<input value={form.ssl_mode} onChange={(e) => setForm({ ...form, ssl_mode: e.target.value })} required /></label><label>凭据引用<input type="password" autoComplete="off" value={form.credential_ref} onChange={(e) => setForm({ ...form, credential_ref: e.target.value })} required /><small>只提交给服务器，列表不会回显</small></label></div><button className="button primary">创建数据源</button></form></Card>}
    {error !== undefined && <ErrorNotice error={error} />}<Card>{result.error && <ErrorNotice error={result.error} />}{!result.data?.items.length ? <EmptyState title="没有数据源" description="管理员可添加受控 SQL 数据源。" /> : <div className="admin-list">{result.data?.items.map((source) => <article key={source.datasource_id}><button className="admin-row" onClick={() => setExpanded(expanded === source.datasource_id ? undefined : source.datasource_id)}><span className="source-icon">DB</span><span className="list-primary"><strong>{source.name}</strong><small>{source.host ? `${source.host}:${source.port}/${source.database_name}` : source.datasource_id}</small></span><StatusBadge status={source.enabled ? "enabled" : "disabled"} /><span>{source.write_ready ? "可写" : "只读 / 未就绪"}</span><span>{expanded === source.datasource_id ? "⌃" : "⌄"}</span></button>{expanded === source.datasource_id && <div className="admin-detail"><div className="resource-meta"><span>凭据：{source.credential_configured ? "已配置" : "未配置"}</span><span>SSL：{source.ssl_mode ?? "—"}</span><button className="button quiet" onClick={() => void toggle(source)}>{source.enabled ? "停用" : "启用"}</button></div><ConnectionEditor source={source} /><PolicyEditor source={source} /></div>}</article>)}</div>}</Card>
  </>;
}
