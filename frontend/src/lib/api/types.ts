export type TaskStatus =
  | "pending"
  | "running"
  | "pausing"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed";

export interface PageInfo {
  limit: number;
  has_more: boolean;
  next_cursor?: string | null;
}

export interface Page<T> {
  items: T[];
  page: PageInfo;
}

export interface AccessSession {
  access_token: string;
  token_type: "Bearer";
  expires_in: number;
  session_id: string;
  user: { user_id: string; email: string };
  tenant: { tenant_id: string; name: string };
  roles: string[];
}

export interface Thread {
  thread_id: string;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

export interface Message {
  message_id: string;
  task_id: string | null;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface ReportReference {
  workspace_path: string;
  content_hash: string;
  version: number;
}

export interface TaskProjection {
  task_id: string;
  thread_id: string;
  message_id?: string;
  task_status: TaskStatus;
  route?: { mode: "direct" | "focused" | "deep"; target_agent?: string };
  pending_write?: string;
  phase: string;
  report: { draft?: ReportReference; final?: ReportReference };
  updated_at: string;
}

export interface CreateTaskInput {
  query: string;
  thread_id?: string;
  options: {
    max_depth: number;
    max_revision_rounds: number;
    datasource_ids: string[];
  };
}

export interface Evidence {
  evidence_id: string;
  hypothesis_id: string;
  summary: string;
  excerpt: string;
  source_type: "web" | "rag" | "sql" | "compute";
  stance: "support" | "refute" | "context";
  relevance: number;
  data?: Record<string, unknown>;
  doc_id?: string;
  chunk_id?: string;
  workspace_path?: string;
  source_uri?: string;
  published_at?: string;
  retrieved_at?: string;
  content_hash: string;
  derived_from?: string[];
  execution_ref?: string;
}

export interface GraphEntity {
  entity_id: string;
  canonical_name: string;
  entity_type: string;
  aliases: string[];
}

export interface GraphClaim {
  claim_id: string;
  claim_key: string;
  predicate: string;
  subject_entity_id?: string;
  object_entity_id?: string;
  value?: string;
  qualifiers: Record<string, string>;
  provenance: Array<{ evidence_id: string; locator?: string }>;
}

export interface GraphResult {
  graph_status: "building" | "ready" | "partial";
  resolution_status?: "hit" | "miss" | "ambiguous";
  candidates: GraphEntity[];
  entities: GraphEntity[];
  claims: GraphClaim[];
  relations: Array<{
    from_entity_id: string;
    to_entity_id?: string;
    predicate: string;
    claim_id: string;
  }>;
  paths: Array<{ entity_ids: string[]; claim_ids: string[]; hops: number }>;
  evidence_ids: string[];
  truncated: boolean;
}

export interface WorkspaceFile {
  workspace_path: string;
  content_hash: string;
  mime_type: string;
  size: number;
  status: string;
  producer_tool_call_id?: string;
}

export type ReportSegment =
  | { type: "text"; text: string }
  | { type: "claim"; claim_id: string; text: string; citation_ids: string[] };

export type ReportBlock =
  | { type: "paragraph"; segments: ReportSegment[] }
  | { type: "list"; ordered: boolean; items: Array<{ segments: ReportSegment[] }> }
  | {
      type: "table";
      caption?: string;
      columns: string[];
      rows: string[][];
      citation_ids: string[];
    }
  | {
      type: "image";
      workspace_path: string;
      content_hash: string;
      alt_text: string;
      caption?: string;
      citation_ids: string[];
    };

export interface ReportAst {
  report_version: number;
  title: string;
  sections: Array<{
    section_id: string;
    heading: string;
    level: number;
    blocks: ReportBlock[];
  }>;
  citations: Array<{ citation_id: string; evidence_id: string; locator?: string }>;
}

export interface ReportResponse {
  task_id: string;
  version: number;
  workspace_path: string;
  content_hash: string;
  report: ReportAst;
}

export interface FilterPredicate {
  column: string;
  op: "=" | "!=" | ">" | ">=" | "<" | "<=" | "IN";
  value: unknown;
}

export interface PendingWrite {
  write_id: string;
  task_id: string;
  plan: {
    plan_id: string;
    datasource_id: string;
    operation: "insert" | "update" | "delete";
    table: string;
    values?: Record<string, unknown>;
    filters?: FilterPredicate[];
    reason: string;
    preconditions?: Record<string, unknown>;
  };
  plan_hash: string;
  preview: {
    datasource: string;
    operation: "insert" | "update" | "delete";
    table: string;
    filters?: FilterPredicate[];
    values?: Record<string, unknown>;
    reason: string;
    current_version?: unknown;
    expected_value?: unknown;
  };
  policy_revision: string;
  status: "pending" | "approved" | "executing" | "rejected" | "executed" | "failed" | "expired" | "stale";
  created_at: string;
  expires_at?: string;
  decision?: { action: string; actor_id: string; at: string; comment?: string; reason?: string };
  result?: { status: string; affected_rows: number; error_code?: string; committed_at?: string; replayed: boolean };
}

export interface DocumentItem {
  doc_id: string;
  name: string;
  media_type: string;
  size_bytes: number;
  content_hash: string;
  status: "pending" | "ready" | "failed";
  tags: string[];
  published_generation: number | null;
  created_at: string;
  updated_at: string;
}

export interface IngestionStatus {
  doc_id: string;
  document_status: string;
  published_generation: number | null;
  ingestion: {
    generation: number;
    status: string;
    stage: string;
    attempt_count: number;
    error_code?: string;
    updated_at: string;
  };
  updated_at: string;
}

export interface DataSource {
  datasource_id: string;
  name: string;
  enabled: boolean;
  access_level: "read" | "write" | null;
  write_ready: boolean;
  host?: string;
  port?: number;
  database_name?: string;
  ssl_mode?: string;
  credential_configured?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface ModelConfig {
  model_config_id: string;
  version: number;
  provider: string;
  model: string;
  base_url?: string | null;
  purpose: "chat" | "embedding" | "rerank";
  secret_configured: boolean;
  enabled_for_new_tasks: boolean;
  revoked: boolean;
  created_at: string;
  updated_at: string;
}

export interface ApiProblemBody {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  instance?: string;
  request_id?: string;
  retryable?: boolean;
  errors?: Array<{ field: string; code: string; message: string }>;
}
