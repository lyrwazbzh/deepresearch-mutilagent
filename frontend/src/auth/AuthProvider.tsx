import { createContext, type PropsWithChildren, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiClient, ApiProblem } from "../lib/api/client";
import type { AccessSession } from "../lib/api/types";

interface AuthValue {
  session: AccessSession | null;
  client: ApiClient;
  ready: boolean;
  login: (email: string, password: string, tenantId: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [session, setSession] = useState<AccessSession | null>(null);
  const [ready, setReady] = useState(false);
  const sessionRef = useRef<AccessSession | null>(null);

  const receiveSession = useCallback((next: AccessSession) => {
    sessionRef.current = next;
    setSession(next);
  }, []);
  const expireSession = useCallback(() => {
    sessionRef.current = null;
    setSession(null);
  }, []);

  const client = useMemo(() => new ApiClient({
    ...(import.meta.env.VITE_API_BASE_URL ? { baseUrl: import.meta.env.VITE_API_BASE_URL } : {}),
    getAccessToken: () => sessionRef.current?.access_token ?? null,
    onSession: receiveSession,
    onSessionExpired: expireSession,
  }), [expireSession, receiveSession]);

  useEffect(() => {
    let active = true;
    client.refresh()
      .catch((error: unknown) => {
        if (!(error instanceof ApiProblem && error.problem.status === 401)) {
          console.info("No resumable browser session.");
        }
      })
      .finally(() => { if (active) setReady(true); });
    return () => { active = false; };
  }, [client]);

  const value = useMemo<AuthValue>(() => ({
    session,
    client,
    ready,
    login: async (email, password, tenantId) => { await client.login(email, password, tenantId); },
    logout: () => client.logout(),
  }), [client, ready, session]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
