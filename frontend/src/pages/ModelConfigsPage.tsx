import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { keys } from "../lib/api/queryKeys";
import type { ModelConfig } from "../lib/api/types";
import { useAuth } from "../auth/AuthProvider";
import { Card, EmptyState, ErrorNotice, PageHeader, StatusBadge, formatDate } from "../components/common/Common";

function ModelConfigActions({ item, onChanged }: { item: ModelConfig; onChanged: () => Promise<void> }) {
  const { client } = useAuth();
  const [editing, setEditing] = useState(false);
  const [provider, setProvider] = useState(item.provider);
  const [model, setModel] = useState(item.model);
  const [baseUrl, setBaseUrl] = useState(item.base_url ?? "");
  const [purpose, setPurpose] = useState(item.purpose);
  const [secret, setSecret] = useState("");
  const [detail, setDetail] = useState<ModelConfig>();
  const [error, setError] = useState<unknown>();
  const save = async (event: FormEvent) => {
    event.preventDefault(); setError(undefined);
    try {
      await client.patchModelConfig(item.model_config_id, { provider, model, purpose, base_url: baseUrl || null, ...(secret ? { secret_ref: secret } : {}) });
      setSecret(""); setEditing(false); await onChanged();
    } catch (cause) { setError(cause); }
  };
  const showDetail = async () => { setError(undefined); try { setDetail(await client.modelConfigVersion(item.model_config_id, item.version)); } catch (cause) { setError(cause); } };
  const revoke = async () => {
    const reason = window.prompt("请输入撤销这个版本的原因");
    if (!reason?.trim()) return;
    setError(undefined);
    try { await client.revokeModelConfigVersion(item.model_config_id, item.version, reason.trim()); await onChanged(); } catch (cause) { setError(cause); }
  };
  return <div className="model-actions"><div className="row-actions"><button className="button quiet" onClick={() => setEditing(!editing)}>编辑新版本</button><button className="button quiet" onClick={() => void showDetail()}>版本详情</button>{!item.revoked && <button className="button danger" onClick={() => void revoke()}>撤销版本</button>}</div>{detail && <div className="version-detail"><strong>{detail.model_config_id} · v{detail.version}</strong><span>{detail.provider} / {detail.model}</span><span>密钥：{detail.secret_configured ? "已配置" : "未配置"}</span><button className="icon-button" onClick={() => setDetail(undefined)}>×</button></div>}{editing && <form className="model-edit-form" onSubmit={(event) => void save(event)}><label>Provider<input value={provider} onChange={(event) => setProvider(event.target.value)} required /></label><label>Model<input value={model} onChange={(event) => setModel(event.target.value)} required /></label><label>用途<select value={purpose} onChange={(event) => setPurpose(event.target.value as typeof purpose)}><option value="chat">chat</option><option value="embedding">embedding</option><option value="rerank">rerank</option></select></label><label>Base URL<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label><label>替换密钥引用<input type="password" autoComplete="off" value={secret} onChange={(event) => setSecret(event.target.value)} placeholder="留空则不变" /></label><button className="button secondary">创建新版本</button></form>}{error !== undefined && <ErrorNotice error={error} />}</div>;
}

export function ModelConfigsPage() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<unknown>();
  const [testing, setTesting] = useState<string>();
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  const [form, setForm] = useState({ model_config_id: "", provider: "", model: "", base_url: "", purpose: "chat" as "chat" | "embedding" | "rerank", secret_ref: "" });
  const result = useQuery({ queryKey: keys.modelConfigs, queryFn: () => client.modelConfigs() });
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(undefined);
    const body = { provider: form.provider, model: form.model, purpose: form.purpose, ...(form.model_config_id ? { model_config_id: form.model_config_id } : {}), ...(form.base_url ? { base_url: form.base_url } : {}), ...(form.secret_ref ? { secret_ref: form.secret_ref } : {}) };
    try { await client.createModelConfig(body); setOpen(false); setForm({ model_config_id: "", provider: "", model: "", base_url: "", purpose: "chat", secret_ref: "" }); await queryClient.invalidateQueries({ queryKey: keys.modelConfigs }); } catch (cause) { setError(cause); }
  };
  const test = async (item: ModelConfig) => { setTesting(item.model_config_id); setError(undefined); try { const value = await client.testModelConfig(item.model_config_id); setTestResult((current) => ({ ...current, [item.model_config_id]: value.status })); } catch (cause) { setError(cause); } finally { setTesting(undefined); } };
  const disable = async (item: ModelConfig) => { try { await client.disableModelConfig(item.model_config_id); await queryClient.invalidateQueries({ queryKey: keys.modelConfigs }); } catch (cause) { setError(cause); } };
  return <><PageHeader eyebrow="MODEL RUNTIME" title="模型配置" description="按版本管理 provider/model，并仅显示密钥是否已配置。" actions={<button className="button primary" onClick={() => setOpen(!open)}>＋ 新建配置</button>} />
    {open && <Card><form className="admin-form" onSubmit={(e) => void submit(e)}><h3>模型配置</h3><div className="form-grid"><label>配置 ID（可选）<input value={form.model_config_id} onChange={(e) => setForm({ ...form, model_config_id: e.target.value })} /></label><label>用途<select value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value as typeof form.purpose })}><option value="chat">chat</option><option value="embedding">embedding</option><option value="rerank">rerank</option></select></label><label>Provider<input value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })} required /></label><label>Model<input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} required /></label><label>Base URL（可选）<input type="url" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></label><label>密钥引用（可选）<input type="password" autoComplete="off" value={form.secret_ref} onChange={(e) => setForm({ ...form, secret_ref: e.target.value })} /><small>服务端只返回是否已配置</small></label></div><button className="button primary">创建版本 1</button></form></Card>}
    {error !== undefined && <ErrorNotice error={error} />}<Card>{result.error && <ErrorNotice error={result.error} />}{!result.data?.items.length ? <EmptyState title="没有模型配置" description="管理员可建立聊天、Embedding 或 Rerank 配置。" /> : <div className="admin-list">{result.data?.items.map((item) => <article className="model-row-wrap" key={`${item.model_config_id}-${item.version}`}><div className="model-row"><span className="model-icon">AI</span><span className="list-primary"><strong>{item.model_config_id} · v{item.version}</strong><small>{item.provider} / {item.model} · {item.purpose}</small></span><span>密钥 {item.secret_configured ? "已配置" : "未配置"}</span><StatusBadge status={item.revoked ? "revoked" : item.enabled_for_new_tasks ? "enabled" : "disabled"} /><span>{formatDate(item.updated_at)}</span><div className="row-actions"><button className="button quiet" disabled={testing === item.model_config_id} onClick={() => void test(item)}>{testing === item.model_config_id ? "测试中…" : testResult[item.model_config_id] ?? "测试"}</button>{item.enabled_for_new_tasks && <button className="button danger" onClick={() => void disable(item)}>停用</button>}</div></div><ModelConfigActions item={item} onChanged={async () => { await queryClient.invalidateQueries({ queryKey: keys.modelConfigs }); }} /></article>)}</div>}</Card>
  </>;
}
