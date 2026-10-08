import type { PropsWithChildren } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../../auth/AuthProvider";

const nav = [
  ["/", "概览", "⌂"],
  ["/workbench", "研究工作台", "◫"],
  ["/history", "研究历史", "◷"],
  ["/documents", "知识文档", "▤"],
  ["/datasources", "数据源", "◈"],
  ["/settings/models", "模型配置", "◎"],
] as const;

export function AppShell({ children }: PropsWithChildren) {
  const { session, logout } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const signOut = async () => {
    await logout();
    queryClient.clear();
    void navigate("/login", { replace: true });
  };
  return <div className="app-shell">
    <aside className="sidebar">
      <NavLink className="brand" to="/">
        <span className="brand-mark">DR</span>
        <span><strong>DeepResearch</strong><small>Research OS</small></span>
      </NavLink>
      <nav aria-label="主导航">
        {nav.map(([href, label, icon]) => <NavLink key={href} to={href} end={href === "/"} className={({ isActive }) => isActive ? "nav-item active" : "nav-item"}>
          <span aria-hidden>{icon}</span>{label}
        </NavLink>)}
      </nav>
      <div className="sidebar-footer">
        <div className="avatar">{session?.user.email.slice(0, 1).toUpperCase()}</div>
        <div className="identity"><strong>{session?.user.email}</strong><small>{session?.tenant.name} · {session?.roles.join(", ")}</small></div>
        <button className="icon-button" onClick={() => void signOut()} title="退出登录" aria-label="退出登录">↪</button>
      </div>
    </aside>
    <main className="main-content">{children}</main>
  </div>;
}
