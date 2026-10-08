import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthProvider";
import { AppShell } from "./components/layout/AppShell";
import { LoginPage } from "./pages/LoginPage";
import { HomePage } from "./pages/HomePage";
import { HistoryPage } from "./pages/HistoryPage";
import { WorkbenchPage } from "./pages/WorkbenchPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { DataSourcesPage } from "./pages/DataSourcesPage";
import { ModelConfigsPage } from "./pages/ModelConfigsPage";

function ProtectedLayout() {
  const { ready, session } = useAuth();
  if (!ready) return <div className="center-screen"><span className="spinner" />正在恢复会话…</div>;
  if (!session) return <Navigate to="/login" replace />;
  return <AppShell><Outlet /></AppShell>;
}

export function App() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route element={<ProtectedLayout />}>
      <Route index element={<HomePage />} />
      <Route path="history" element={<HistoryPage />} />
      <Route path="workbench" element={<WorkbenchPage />} />
      <Route path="threads/:threadId" element={<WorkbenchPage />} />
      <Route path="threads/:threadId/tasks/:taskId" element={<WorkbenchPage />} />
      <Route path="documents" element={<DocumentsPage />} />
      <Route path="datasources" element={<DataSourcesPage />} />
      <Route path="settings/models" element={<ModelConfigsPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>;
}
