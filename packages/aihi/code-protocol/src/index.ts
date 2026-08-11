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
  scope: "tui" | "session" | "task" | "config" | "integration";
  execution: "local" | "worker";
  mutates: boolean;
  requires_approval: boolean;
}
