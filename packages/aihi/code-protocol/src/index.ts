/** Versioned JSON-RPC contract between the TypeScript CLI and Python Worker. */

export const PROTOCOL_VERSION = "0.2" as const;

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
  run_id: string;
  accepted: true;
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
  requested_by?: string;
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
  protocol_version: typeof PROTOCOL_VERSION;
  server_name: string;
  capabilities: {
    events: boolean;
    commands: CommandDescriptor[];
  };
  storage?: StorageDescriptor;
}

export interface InitializeParams extends JsonObject {
  protocol_version: typeof PROTOCOL_VERSION;
  client_name?: string;
  capabilities?: JsonObject;
  store_path?: string;
}

export interface SessionCreateParams extends JsonObject {
  cwd: string;
  provider?: string;
  model?: string;
  session_id?: string;
  metadata?: JsonObject;
}

export interface TaskCreateParams extends JsonObject {
  session_id: string;
  parent_run_id: string;
  objective: string;
  budget?: JsonObject;
  workspace?: JsonObject;
  capabilities?: string[];
  constraints?: string[];
  max_depth?: number;
  max_children?: number;
  metadata?: JsonObject;
}

export interface TaskSpawnParams extends JsonObject {
  session_id: string;
  parent_task_id: string;
  objective: string;
  budget?: JsonObject;
  workspace?: JsonObject;
  capabilities?: string[];
  constraints?: string[];
  metadata?: JsonObject;
}

export interface TaskTransitionParams extends JsonObject {
  session_id: string;
  task_id: string;
  state: Exclude<TaskState, "pending">;
  reason?: string;
  summary?: string;
  error?: string;
  output_artifact_ids?: string[];
  metrics?: JsonObject;
}

export interface RunStartParams extends JsonObject {
  session_id: string;
  user_message: string;
  run_id?: string;
  provider?: string;
  model?: string;
  max_output_tokens?: number;
}

export interface RunResumeParams extends JsonObject {
  session_id: string;
  run_id: string;
  model?: string;
  max_output_tokens?: number;
}

export interface RunCancelParams extends JsonObject {
  session_id: string;
  run_id: string;
  reason?: string;
}

export interface ApprovalResolveParams extends JsonObject {
  session_id: string;
  approval_id: string;
  approved: boolean;
  one_shot?: boolean;
  resolved_by?: string;
}

export interface ApprovalResolution extends JsonObject {
  session_id: string;
  approval_id: string;
  run_id: string;
  approved: boolean;
  one_shot: boolean;
}

export interface HostAcknowledgement extends JsonObject {
  path: string;
  workspace: string;
  root: string;
  acknowledged: true;
}

export interface EventNotification<TData = Record<string, unknown>>
  extends JsonRpcNotification<{ protocol_version: string; event: AgentEvent<TData> }> {
  method: "event";
  params: { protocol_version: typeof PROTOCOL_VERSION; event: AgentEvent<TData> };
}

export interface RunError extends JsonObject {
  session_id: string;
  run_id: string;
  message: string;
}

export interface RunErrorNotification
  extends JsonRpcNotification<RunError & { protocol_version: typeof PROTOCOL_VERSION }> {
  method: "run.error";
  params: RunError & { protocol_version: typeof PROTOCOL_VERSION };
}

export interface CommandDescriptor {
  name: string;
  aliases: string[];
  scope: "tui" | "session" | "task" | "run" | "approval" | "skill" | "config" | "integration";
  execution: "local" | "worker";
  mutates: boolean;
  requires_approval: boolean;
}

interface RpcMethod<TParams extends JsonObject, TResult> {
  params: TParams;
  result: TResult;
}

/** Complete request/response map advertised by the 0.2 local Worker. */
export interface CodeRpcMethodMap {
  initialize: RpcMethod<InitializeParams, InitializeResult>;
  shutdown: RpcMethod<JsonObject, { ok: true }>;
  "session.create": RpcMethod<SessionCreateParams, { session: SessionDescriptor }>;
  "session.list": RpcMethod<{ limit?: number }, { sessions: SessionDescriptor[] }>;
  "session.get": RpcMethod<{ session_id: string }, { session: SessionDescriptor }>;
  "session.events": RpcMethod<
    { session_id: string; after_seq?: number; limit?: number },
    SessionEventsResult
  >;
  "session.fork": RpcMethod<
    { session_id: string; at_seq?: number },
    { session: SessionDescriptor; at_seq: number }
  >;
  "session.usage": RpcMethod<{ session_id: string }, SessionUsage>;
  "task.create": RpcMethod<TaskCreateParams, { task: TaskDescriptor }>;
  "task.spawn": RpcMethod<TaskSpawnParams, { task: TaskDescriptor }>;
  "task.get": RpcMethod<
    { session_id: string; task_id: string },
    { task: TaskDescriptor }
  >;
  "task.list": RpcMethod<
    { session_id: string; active_only?: boolean },
    { tasks: TaskDescriptor[] }
  >;
  "task.transition": RpcMethod<TaskTransitionParams, { task: TaskDescriptor }>;
  "run.start": RpcMethod<RunStartParams, RunAccepted>;
  "run.resume": RpcMethod<RunResumeParams, RunAccepted>;
  "run.list": RpcMethod<{ session_id: string }, { runs: RunDescriptor[] }>;
  "run.cancel": RpcMethod<RunCancelParams, RunCancelResult>;
  "config.get": RpcMethod<{ cwd?: string }, { config: ConfigDescriptor }>;
  "config.init": RpcMethod<JsonObject, { path: string; created: boolean }>;
  "config.acknowledge_host": RpcMethod<
    { cwd: string; acknowledged: true },
    HostAcknowledgement
  >;
  "approval.list": RpcMethod<
    { session_id: string; run_id?: string },
    { approvals: ApprovalDescriptor[] }
  >;
  "approval.resolve": RpcMethod<ApprovalResolveParams, ApprovalResolution>;
  "skill.list": RpcMethod<{ session_id: string }, { skills: SkillDescriptor[] }>;
  "skill.trust": RpcMethod<
    { session_id: string; name: string; enable: boolean; trusted_by: string },
    { skill: JsonObject }
  >;
  "skill.untrust": RpcMethod<
    { session_id: string; name: string },
    { removed: boolean }
  >;
  "mcp.list": RpcMethod<
    { session_id: string },
    { servers: McpServerDescriptor[] }
  >;
  "tool.list": RpcMethod<{ session_id: string }, { tools: ToolDescriptor[] }>;
}

export type CodeRpcMethod = keyof CodeRpcMethodMap;
export type CodeRpcParams<TMethod extends CodeRpcMethod> =
  CodeRpcMethodMap[TMethod]["params"];
export type CodeRpcResult<TMethod extends CodeRpcMethod> =
  CodeRpcMethodMap[TMethod]["result"];

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

export function isAgentEvent(value: unknown): value is AgentEvent {
  if (!isJsonObject(value)) return false;
  return (
    isNonEmptyText(value.session_id) &&
    isNonEmptyText(value.event_type) &&
    typeof value.ephemeral === "boolean" &&
    isJsonObject(value.data) &&
    (value.run_id === undefined || isNonEmptyText(value.run_id)) &&
    (value.ephemeral || value.seq !== undefined) &&
    (value.seq === undefined ||
      (typeof value.seq === "number" && Number.isSafeInteger(value.seq) && value.seq > 0))
  );
}

export function isEventNotification(value: unknown): value is EventNotification {
  if (!isJsonObject(value) || value.jsonrpc !== "2.0" || value.method !== "event") {
    return false;
  }
  const params = value.params;
  return (
    isJsonObject(params) &&
    params.protocol_version === PROTOCOL_VERSION &&
    isAgentEvent(params.event)
  );
}

export function isRunAccepted(value: unknown): value is RunAccepted {
  return isJsonObject(value) && value.accepted === true && isNonEmptyText(value.run_id);
}

export function isApprovalDescriptor(value: unknown): value is ApprovalDescriptor {
  if (!isJsonObject(value)) return false;
  return (
    isNonEmptyText(value.approval_id) &&
    isNonEmptyText(value.scope) &&
    isNonEmptyText(value.granted_by) &&
    (value.requested_by === undefined || isNonEmptyText(value.requested_by)) &&
    (value.expires_at === null || typeof value.expires_at === "string") &&
    (value.run_id === null || isNonEmptyText(value.run_id)) &&
    typeof value.one_shot === "boolean" &&
    (value.tool_call_id === undefined || isNonEmptyText(value.tool_call_id)) &&
    (value.tool_name === undefined || isNonEmptyText(value.tool_name)) &&
    (value.tool_input === undefined || isJsonObject(value.tool_input)) &&
    (value.reason === undefined || typeof value.reason === "string") &&
    (value.rule_id === undefined || typeof value.rule_id === "string") &&
    (value.required_capabilities === undefined ||
      (Array.isArray(value.required_capabilities) &&
        value.required_capabilities.every((capability) => typeof capability === "string"))) &&
    (value.sandbox === undefined || isJsonObject(value.sandbox))
  );
}

export function isApprovalResolution(value: unknown): value is ApprovalResolution {
  return (
    isJsonObject(value) &&
    isNonEmptyText(value.session_id) &&
    isNonEmptyText(value.approval_id) &&
    isNonEmptyText(value.run_id) &&
    typeof value.approved === "boolean" &&
    typeof value.one_shot === "boolean"
  );
}

function isCommandDescriptor(value: unknown): value is CommandDescriptor {
  if (!isJsonObject(value)) return false;
  return (
    isNonEmptyText(value.name) &&
    Array.isArray(value.aliases) &&
    value.aliases.every((alias) => typeof alias === "string") &&
    ["tui", "session", "task", "run", "approval", "skill", "config", "integration"].includes(
      String(value.scope),
    ) &&
    ["local", "worker"].includes(String(value.execution)) &&
    typeof value.mutates === "boolean" &&
    typeof value.requires_approval === "boolean"
  );
}

export function isInitializeResult(value: unknown): value is InitializeResult {
  if (!isJsonObject(value) || value.protocol_version !== PROTOCOL_VERSION) return false;
  const capabilities = value.capabilities;
  if (!isJsonObject(capabilities)) return false;
  if (
    !isNonEmptyText(value.server_name) ||
    typeof capabilities.events !== "boolean" ||
    !Array.isArray(capabilities.commands) ||
    !capabilities.commands.every(isCommandDescriptor)
  ) {
    return false;
  }
  if (value.storage === undefined) return true;
  if (!isJsonObject(value.storage)) return false;
  return (
    (value.storage.kind === "memory" || value.storage.kind === "sqlite") &&
    (value.storage.path === undefined ||
      value.storage.path === null ||
      typeof value.storage.path === "string")
  );
}

export function isEventRecord(value: unknown): value is EventRecord {
  if (!isJsonObject(value)) return false;
  return (
    isNonEmptyText(value.id) &&
    isNonEmptyText(value.type) &&
    isNonEmptyText(value.session_id) &&
    (value.run_id === null || isNonEmptyText(value.run_id)) &&
    (value.seq === null ||
      (typeof value.seq === "number" && Number.isSafeInteger(value.seq) && value.seq > 0)) &&
    isNonEmptyText(value.created_at) &&
    typeof value.ephemeral === "boolean" &&
    typeof value.schema_version === "number" &&
    Number.isSafeInteger(value.schema_version) &&
    value.schema_version > 0 &&
    isJsonObject(value.data)
  );
}

export function isSessionEventsResult(value: unknown): value is SessionEventsResult {
  if (!isJsonObject(value)) return false;
  return (
    isNonEmptyText(value.session_id) &&
    Array.isArray(value.events) &&
    value.events.every(isEventRecord) &&
    typeof value.head_seq === "number" &&
    Number.isSafeInteger(value.head_seq) &&
    value.head_seq >= 0 &&
    typeof value.next_after_seq === "number" &&
    Number.isSafeInteger(value.next_after_seq) &&
    value.next_after_seq >= 0 &&
    typeof value.has_more === "boolean"
  );
}

export function isRunErrorNotification(value: unknown): value is RunErrorNotification {
  if (!isJsonObject(value) || value.jsonrpc !== "2.0" || value.method !== "run.error") {
    return false;
  }
  const params = value.params;
  return (
    isJsonObject(params) &&
    params.protocol_version === PROTOCOL_VERSION &&
    isNonEmptyText(params.session_id) &&
    isNonEmptyText(params.run_id) &&
    isNonEmptyText(params.message)
  );
}
