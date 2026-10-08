import type {
  AccessSession,
  ApiProblemBody,
  CreateTaskInput,
  DataSource,
  DocumentItem,
  Evidence,
  GraphResult,
  IngestionStatus,
  Message,
  ModelConfig,
  Page,
  PendingWrite,
  ReportResponse,
  TaskProjection,
  Thread,
  WorkspaceFile,
} from "./types";

export class ApiProblem extends Error {
  readonly problem: ApiProblemBody;

  constructor(problem: ApiProblemBody) {
    super(problem.detail || problem.title);
    this.name = "ApiProblem";
    this.problem = problem;
  }
}

export interface ApiClientOptions {
  baseUrl?: string;
  getAccessToken: () => string | null;
  onSession: (session: AccessSession) => void;
  onSessionExpired: () => void;
  fetchImpl?: typeof fetch;
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  idempotencyKey?: string;
  retryAuth?: boolean;
}

const fallbackProblem = (status: number, detail: string): ApiProblemBody => ({
  type: "about:blank",
  title: "Request failed",
  status,
  code: "http_error",
  detail,
});

const csrfToken = () => typeof document === "undefined"
  ? null
  : document.cookie.split("; ").find((item) => item.startsWith("dr_csrf="))?.split("=").slice(1).join("=") ?? null;

export const idempotencyKey = () => crypto.randomUUID();

export class ApiClient {
  readonly baseUrl: string;
  private readonly getAccessToken: () => string | null;
  private readonly onSession: (session: AccessSession) => void;
  private readonly onSessionExpired: () => void;
  private readonly fetchImpl: typeof fetch;
  private refreshFlight: Promise<AccessSession> | null = null;

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl?.replace(/\/$/, "") ?? "";
    this.getAccessToken = options.getAccessToken;
    this.onSession = options.onSession;
    this.onSessionExpired = options.onSessionExpired;
    this.fetchImpl = options.fetchImpl ?? fetch.bind(globalThis);
  }

  async login(email: string, password: string, tenantId: string): Promise<AccessSession> {
    const response = await this.raw("/api/v1/auth/login", {
      method: "POST",
      body: { email, password, tenant_id: tenantId },
      retryAuth: false,
    }, false);
    const session = await this.json<AccessSession>(response);
    this.onSession(session);
    return session;
  }

  refresh(): Promise<AccessSession> {
    if (!this.refreshFlight) {
      this.refreshFlight = this.raw("/api/v1/auth/refresh", {
        method: "POST",
        retryAuth: false,
      }, false)
        .then((response) => this.json<AccessSession>(response))
        .then((session) => {
          this.onSession(session);
          return session;
        })
        .catch((error: unknown) => {
          this.onSessionExpired();
          throw error;
        })
        .finally(() => {
          this.refreshFlight = null;
        });
    }
    return this.refreshFlight;
  }

  async logout(): Promise<void> {
    try {
      await this.raw("/api/v1/auth/logout", { method: "POST", retryAuth: false }, false);
    } finally {
      this.onSessionExpired();
    }
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const response = await this.raw(path, options, true);
    if (response.status === 204) return undefined as T;
    return this.json<T>(response);
  }

  async blob(path: string): Promise<Blob> {
    const response = await this.raw(path, {}, true);
    return response.blob();
  }

  async stream(path: string, init: RequestInit = {}): Promise<Response> {
    const request = () => {
      const headers = new Headers(init.headers);
      headers.set("Accept", "text/event-stream");
      const token = this.getAccessToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      return this.fetchImpl(`${this.baseUrl}${path}`, { ...init, credentials: "include", headers });
    };
    let response = await request();
    if (response.status === 401) {
      await this.refresh();
      response = await request();
    }
    if (!response.ok) await this.throwProblem(response);
    return response;
  }

  async upload<T>(path: string, form: FormData, key: string, onProgress?: (value: number) => void): Promise<T> {
    const attempt = () => new Promise<T>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${this.baseUrl}${path}`);
      xhr.withCredentials = true;
      xhr.setRequestHeader("Idempotency-Key", key);
      const token = this.getAccessToken();
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText) as T); }
          catch { reject(new Error("The upload response was not valid JSON.")); }
          return;
        }
        try { reject(new ApiProblem(JSON.parse(xhr.responseText) as ApiProblemBody)); }
        catch { reject(new ApiProblem(fallbackProblem(xhr.status, xhr.statusText))); }
      };
      xhr.onerror = () => reject(new Error("Network error while uploading."));
      xhr.send(form);
    });
    try {
      return await attempt();
    } catch (error) {
      if (error instanceof ApiProblem && error.problem.status === 401) {
        await this.refresh();
        return attempt();
      }
      throw error;
    }
  }

  private async raw(path: string, options: RequestOptions, includeAuth: boolean): Promise<Response> {
    const { body, idempotencyKey: key, retryAuth = true, headers: sourceHeaders, ...init } = options;
    const execute = () => {
      const headers = new Headers(sourceHeaders);
      headers.set("Accept", "application/json");
      if (body !== undefined && !(body instanceof FormData)) headers.set("Content-Type", "application/json");
      if (key) headers.set("Idempotency-Key", key);
      const csrf = csrfToken();
      if (!includeAuth && csrf) headers.set("X-CSRF-Token", decodeURIComponent(csrf));
      const token = includeAuth ? this.getAccessToken() : null;
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const requestInit: RequestInit = {
        ...init,
        credentials: "include",
        headers,
      };
      if (body !== undefined) requestInit.body = body instanceof FormData ? body : JSON.stringify(body);
      return this.fetchImpl(`${this.baseUrl}${path}`, requestInit);
    };
    let response = await execute();
    if (includeAuth && retryAuth && response.status === 401) {
      await this.refresh();
      response = await execute();
    }
    if (!response.ok) await this.throwProblem(response);
    return response;
  }

  private async json<T>(response: Response): Promise<T> {
    return response.json() as Promise<T>;
  }

  private async throwProblem(response: Response): Promise<never> {
    let value: ApiProblemBody;
    try { value = await response.json() as ApiProblemBody; }
    catch { value = fallbackProblem(response.status, response.statusText); }
    throw new ApiProblem(value);
  }

  threads = () => this.request<Page<Thread>>("/api/v1/threads?limit=100");
  createThread = () => this.request<Thread>("/api/v1/threads", { method: "POST", body: {} });
  messages = (threadId: string) => this.request<Page<Message>>(`/api/v1/threads/${encodeURIComponent(threadId)}/messages?limit=100`);
  tasks = (threadId?: string) => this.request<Page<TaskProjection>>(`/api/v1/tasks?limit=100${threadId ? `&thread_id=${encodeURIComponent(threadId)}` : ""}`);
  task = (taskId: string) => this.request<TaskProjection>(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
  createTask = (input: CreateTaskInput, key: string) => this.request<TaskProjection>("/api/v1/tasks", { method: "POST", body: input, idempotencyKey: key });
  taskCommand = (taskId: string, command: "pause" | "resume" | "cancel", key: string) => this.request<TaskProjection>(`/api/v1/tasks/${encodeURIComponent(taskId)}/${command}`, { method: "POST", idempotencyKey: key });
  replan = (taskId: string, instruction: string, key: string) => this.request<TaskProjection>(`/api/v1/tasks/${encodeURIComponent(taskId)}/replan`, { method: "POST", body: { instruction }, idempotencyKey: key });
  evidence = (taskId: string) => this.request<Page<Evidence>>(`/api/v1/tasks/${encodeURIComponent(taskId)}/evidence?limit=100`);
  evidenceItem = (taskId: string, evidenceId: string) => this.request<Evidence>(`/api/v1/tasks/${encodeURIComponent(taskId)}/evidence/${encodeURIComponent(evidenceId)}`);
  report = (taskId: string) => this.request<ReportResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/report`);
  workspace = (taskId: string) => this.request<{ files: WorkspaceFile[] }>(`/api/v1/tasks/${encodeURIComponent(taskId)}/workspace`);
  workspaceBlob = (taskId: string, path: string) => this.blob(`/api/v1/tasks/${encodeURIComponent(taskId)}/workspace/${path.split("/").map(encodeURIComponent).join("/")}`);
  graph = (taskId: string, body: Record<string, unknown>) => this.request<GraphResult>(`/api/v1/tasks/${encodeURIComponent(taskId)}/knowledge-graph/query`, { method: "POST", body });
  pendingWrite = (taskId: string, writeId: string) => this.request<PendingWrite>(`/api/v1/tasks/${encodeURIComponent(taskId)}/pending-writes/${encodeURIComponent(writeId)}`);
  approveWrite = (taskId: string, writeId: string, planHash: string, comment: string, key: string) => this.request<PendingWrite>(`/api/v1/tasks/${encodeURIComponent(taskId)}/pending-writes/${encodeURIComponent(writeId)}/approve`, { method: "POST", body: { plan_hash: planHash, comment: comment || undefined }, idempotencyKey: key });
  rejectWrite = (taskId: string, writeId: string, reason: string, key: string) => this.request<PendingWrite>(`/api/v1/tasks/${encodeURIComponent(taskId)}/pending-writes/${encodeURIComponent(writeId)}/reject`, { method: "POST", body: { reason: reason || undefined }, idempotencyKey: key });
  documents = () => this.request<Page<DocumentItem>>("/api/v1/documents?limit=100");
  documentBlob = (docId: string) => this.blob(`/api/v1/documents/${encodeURIComponent(docId)}/content?disposition=attachment`);
  ingestion = (docId: string) => this.request<IngestionStatus>(`/api/v1/documents/${encodeURIComponent(docId)}/ingestion`);
  retryIngestion = (docId: string) => this.request(`/api/v1/documents/${encodeURIComponent(docId)}/ingestion/retry`, { method: "POST" });
  rebuildDocument = (docId: string) => this.request(`/api/v1/documents/${encodeURIComponent(docId)}/ingestion/rebuild`, { method: "POST" });
  deleteDocument = (docId: string) => this.request<void>(`/api/v1/documents/${encodeURIComponent(docId)}`, { method: "DELETE" });
  dataSources = () => this.request<Page<DataSource>>("/api/v1/datasources");
  createDataSource = (body: Record<string, unknown>) => this.request<DataSource>("/api/v1/datasources", { method: "POST", body });
  patchDataSource = (id: string, body: Record<string, unknown>) => this.request<DataSource>(`/api/v1/datasources/${encodeURIComponent(id)}`, { method: "PATCH", body });
  toggleDataSource = (id: string, enabled: boolean) => this.request<DataSource>(`/api/v1/datasources/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}`, { method: "POST" });
  replacePolicies = (id: string, policies: unknown[]) => this.request<void>(`/api/v1/datasources/${encodeURIComponent(id)}/policies`, { method: "PUT", body: { policies } });
  putGrant = (id: string, userId: string, accessLevel: "read" | "write") => this.request<void>(`/api/v1/datasources/${encodeURIComponent(id)}/grants/${encodeURIComponent(userId)}`, { method: "PUT", body: { access_level: accessLevel } });
  deleteGrant = (id: string, userId: string) => this.request<void>(`/api/v1/datasources/${encodeURIComponent(id)}/grants/${encodeURIComponent(userId)}`, { method: "DELETE" });
  modelConfigs = () => this.request<Page<ModelConfig>>("/api/v1/model-configs");
  createModelConfig = (body: Record<string, unknown>) => this.request<ModelConfig>("/api/v1/model-configs", { method: "POST", body });
  modelConfigVersion = (id: string, version: number) => this.request<ModelConfig>(`/api/v1/model-configs/${encodeURIComponent(id)}/versions/${version}`);
  patchModelConfig = (id: string, body: Record<string, unknown>) => this.request<ModelConfig>(`/api/v1/model-configs/${encodeURIComponent(id)}`, { method: "PATCH", body });
  testModelConfig = (id: string) => this.request<{ status: string }>(`/api/v1/model-configs/${encodeURIComponent(id)}/test`, { method: "POST" });
  disableModelConfig = (id: string) => this.request<ModelConfig>(`/api/v1/model-configs/${encodeURIComponent(id)}/disable`, { method: "POST" });
  revokeModelConfigVersion = (id: string, version: number, reason: string) => this.request<{ model_config_id: string; version: number; revoked_at: string; reason: string }>(`/api/v1/model-configs/${encodeURIComponent(id)}/versions/${version}/revoke`, { method: "POST", body: { reason } });
}
