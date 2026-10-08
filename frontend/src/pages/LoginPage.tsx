import { type FormEvent, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { ErrorNotice } from "../components/common/Common";

export function LoginPage() {
  const { session, ready, login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const navigate = useNavigate();
  const location = useLocation();

  if (ready && session) return <Navigate to="/" replace />;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true); setError(undefined);
    try {
      await login(email, password, tenantId);
      const next = (location.state as { from?: string } | null)?.from ?? "/";
      void navigate(next, { replace: true });
    } catch (reason) { setError(reason); }
    finally { setBusy(false); }
  };
  return <main className="login-page">
    <section className="login-art">
      <div className="constellation" aria-hidden><i /><i /><i /><i /><i /></div>
      <div className="login-art-copy"><span className="eyebrow light">INTELLIGENCE, GROUNDED</span><h1>把问题变成<br />可验证的研究。</h1><p>多智能体研究、结构化证据与可追溯报告，在同一个工作空间持续推进。</p></div>
    </section>
    <section className="login-panel">
      <form className="login-form" onSubmit={(event) => void submit(event)}>
        <span className="brand-mark large">DR</span>
        <h2>欢迎回来</h2><p>登录你的研究工作台</p>
        {error !== undefined && <ErrorNotice error={error} />}
        <label>租户 ID<input autoComplete="organization" value={tenantId} onChange={(e) => setTenantId(e.target.value)} placeholder="tenant_default" required /></label>
        <label>邮箱<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="researcher@example.com" required /></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <button className="button primary wide" disabled={busy}>{busy ? "正在登录…" : "进入工作台"}</button>
        <small className="security-note">访问凭证仅保存在当前页面内存中</small>
      </form>
    </section>
  </main>;
}
