/** Versioned JSON-RPC contract between the TypeScript CLI and Python Worker. */

export const PROTOCOL_VERSION = "0.1" as const;

export type JsonObject = Record<string, unknown>;
export type JsonRpcId = string | number;

export interface JsonRpcRequest<TParams extends Record<string, unknown> = Record<string, unknown>> {
  jsonrpc: "2.0";
  id: JsonRpcId;
  method: string;
  params?: TParams;
}

export interface JsonRpcNotification<
  TParams extends Record<string, unknown> = Record<string, unknown>,
> {
  jsonrpc: "2.0";
  method: string;
  params?: TParams;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcResponse<TResult = unknown> {
  jsonrpc: "2.0";
  id: JsonRpcId | null;
  result?: TResult;
  error?: JsonRpcError;
}

export interface AgentEvent<TData = Record<string, unknown>> {
  session_id: string;
  run_id?: string;
  event_type: string;
  seq?: number;
  ephemeral: boolean;
  data: TData;
}

export interface EventRecord<TData = Record<string, unknown>> {
  id: string;
  type: string;
  session_id: string;
  run_id: string | null;
  seq: number | null;
  created_at: string;
  ephemeral: boolean;
  schema_version: number;
  data: TData;
}

export interface SessionDescriptor {
  session_id: string;
  head_seq: number;
  created_at: string;
  metadata: JsonObject;
  parent_session_id: string | null;
}

export interface SessionUsage {
  session_id: string;
  model: string;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  context_tokens: number;
  context_limit: number;
  context_used_ratio: number;
}

export interface SessionEventsResult {
  session_id: string;
  events: EventRecord[];
  head_seq: number;
  next_after_seq: number;
  has_more: boolean;
}

export type TaskState =
  | "pending"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type RunState =
  | "created"
  | "running"
  | "waiting_tool"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "interrupted"
  | "cancelled";

/** run.start and run.resume acknowledge immediately; the outcome arrives as events. */
export interface RunAccepted {
  run_id: string | null;
  accepted: boolean;
}

export interface RunResult {
  run_id: string;
  state: RunState;
  suspended: boolean;
  error: string | null;
  pending_approval_id: string | null;
  pending_tool_call_ids: string[];
  response?: {
    message: JsonObject;
    stop_reason: string;
    usage: JsonObject;
  };
}

export interface RunDescriptor {
  run_id: string;
  state: RunState | "created";
  started_at: string;
  updated_at: string;
  provider: string | null;
  model: string | null;
  error: string | null;
  pending_approval_id: string | null;
}

export interface RunCancelResult {
  run_id: string;
  requested?: boolean;
  state?: RunState;
  suspended?: boolean;
  error?: string | null;
}

export interface ProviderDescriptor {
  name: string;
  model: string;
  api_key_env?: string | null;
  base_url?: string | null;
}

export interface ConfigDescriptor extends JsonObject {
  source_path: string | null;
  source_paths: string[];
  base_dir: string;
  provider: ProviderDescriptor;
  providers: ProviderDescriptor[];
  tools: string[];
  sandbox: {
    backend: string;
    root: string;
    unsafe: boolean;
  };
  skills: JsonObject;
  mcp_servers: string[];
}

export interface ApprovalDescriptor extends JsonObject {
  approval_id: string;
  scope: string;
  granted_by: string;
  expires_at: string | null;
  run_id: string | null;
  one_shot: boolean;
  tool_call_id?: string;
  tool_name?: string;
  /** Bounded, credential-redacted preview; never use this value for execution. */
  tool_input?: JsonObject;
  reason?: string;
  rule_id?: string;
  required_capabilities?: string[];
  sandbox?: JsonObject;
}

export interface SkillDescriptor {
  name: string;
  version: string;
  scope: string;
  path: string;
  content_sha256: string;
  trusted: boolean;
  enabled: boolean;
  loadable: boolean;
}

export interface McpServerDescriptor {
  name: string;
  command: string[];
  cwd: string | null;
  env_keys: string[];
  allowed_tools: string[] | null;
  request_timeout_seconds: number;
  reconnect_attempts: number;
  applies_to: "new_run";
}

export interface ToolDescriptor {
  name: string;
  configured: boolean;
}

export interface TaskDescriptor {
  spec: JsonObject;
  state: TaskState;
  child_task_ids: string[];
  result: JsonObject | null;
  reason: string | null;
  updated_at: string;
}

export interface StorageDescriptor {
  kind: "memory" | "sqlite";
  path?: string | null;
}

export interface InitializeResult {
  protocol_version: string;
  server_name: string;
  capabilities: {
    events: boolean;
    commands: CommandDescriptor[];
  };
  storage?: StorageDescriptor;
}

export interface EventNotification<TData = Record<string, unknown>>
  extends JsonRpcNotification<{ protocol_version: string; event: AgentEvent<TData> }> {
  method: "event";
}

export interface CommandDescriptor {
  name: string;
  aliases: string[];
  scope: "tui" | "session" | "task" | "run" | "approval" | "skill" | "config" | "integration";
  execution: "local" | "worker";
  mutates: boolean;
  requires_approval: boolean;
}
