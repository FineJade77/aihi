import type { ChildProcessWithoutNullStreams } from "node:child_process";
import type {
  AgentEvent,
  ApprovalDescriptor,
  EventNotification,
  InitializeResult,
  JsonObject,
  JsonRpcError,
  JsonRpcId,
  JsonRpcRequest,
  SessionDescriptor,
  SessionEventsResult,
  SkillDescriptor,
  RunResult,
  TaskDescriptor,
  TaskState,
} from "@aihi/code-protocol";
import { PROTOCOL_VERSION } from "@aihi/code-protocol";
import { ContentLengthDecoder, encodeFrame } from "./framing.js";
import { launchWorker, type WorkerLaunchOptions } from "../worker/launcher.js";

export interface RpcClientOptions extends WorkerLaunchOptions {
  requestTimeoutMs?: number;
  storePath?: string;
  configPath?: string;
  onEvent?: (event: AgentEvent) => void;
  onLog?: (chunk: string) => void;
  onExit?: (code: number | null, signal: NodeJS.Signals | null) => void;
}

export class RpcError extends Error {
  public readonly code: number;
  public readonly data: unknown;

  public constructor(error: JsonRpcError) {
    super(error.message);
    this.name = "RpcError";
    this.code = error.code;
    this.data = error.data;
  }
}

export interface SessionCreateParams {
  cwd: string;
  provider: string;
  model: string;
  session_id?: string;
  metadata?: JsonObject;
}

export interface TaskCreateParams {
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

export interface TaskSpawnParams {
  session_id: string;
  parent_task_id: string;
  objective: string;
  budget?: JsonObject;
  workspace?: JsonObject;
  capabilities?: string[];
  constraints?: string[];
  metadata?: JsonObject;
}

export interface TaskTransitionParams {
  session_id: string;
  task_id: string;
  state: Exclude<TaskState, "pending">;
  reason?: string;
  summary?: string;
  error?: string;
  output_artifact_ids?: string[];
  metrics?: JsonObject;
}

export interface RunStartParams {
  session_id: string;
  user_message: string;
  run_id?: string;
  model?: string;
  system_prompt?: string;
  max_output_tokens?: number;
}

export interface RunResumeParams {
  session_id: string;
  run_id: string;
  model?: string;
  system_prompt?: string;
  max_output_tokens?: number;
}

export interface ApprovalResolveParams {
  session_id: string;
  approval_id: string;
  approved: boolean;
  one_shot?: boolean;
  resolved_by?: string;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

/** Minimal JSON-RPC client for the local Python Worker lifecycle. */
export class RpcClient {
  private readonly decoder = new ContentLengthDecoder();
  private readonly pending = new Map<string, PendingRequest>();
  private readonly requestTimeoutMs: number;
  private readonly storePath?: string;
  private readonly configPath?: string;
  private readonly onEventCallback?: (event: AgentEvent) => void;
  private readonly onLogCallback?: (chunk: string) => void;
  private readonly onExitCallback?: (code: number | null, signal: NodeJS.Signals | null) => void;
  private readonly eventListeners = new Set<(event: AgentEvent) => void>();
  private nextId = 1;
  private closed = false;
  private initialized = false;
  private initializationResult?: InitializeResult;
  private exitPromise: Promise<void>;
  private resolveExit!: () => void;

  private constructor(
    private readonly child: ChildProcessWithoutNullStreams,
    options: RpcClientOptions,
  ) {
    this.requestTimeoutMs = options.requestTimeoutMs ?? 30_000;
    if (!Number.isSafeInteger(this.requestTimeoutMs) || this.requestTimeoutMs <= 0) {
      child.kill();
      throw new TypeError("requestTimeoutMs must be a positive safe integer");
    }
    this.storePath = options.storePath;
    this.configPath = options.configPath;
    this.onEventCallback = options.onEvent;
    this.onLogCallback = options.onLog;
    this.onExitCallback = options.onExit;
    this.exitPromise = new Promise<void>((resolve) => {
      this.resolveExit = resolve;
    });
    this.attachProcessListeners();
  }

  public static async connect(options: RpcClientOptions = {}): Promise<RpcClient> {
    const client = new RpcClient(launchWorker(options), options);
    try {
      await client.initialize();
      return client;
    } catch (error) {
      await client.close().catch(() => undefined);
      throw error;
    }
  }

  public get process(): ChildProcessWithoutNullStreams {
    return this.child;
  }

  public get isInitialized(): boolean {
    return this.initialized;
  }

  /** Subscribe to Worker event notifications; returns an idempotent unsubscribe. */
  public subscribeEvents(listener: (event: AgentEvent) => void): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  public async initialize(clientName = "aihi-code-cli"): Promise<InitializeResult> {
    if (this.initialized) {
      return (
        this.initializationResult ?? {
          protocol_version: PROTOCOL_VERSION,
          server_name: "aihi-code-agent",
          capabilities: { events: true, commands: [] },
        }
      );
    }
    const initializeParams: JsonObject = {
      protocol_version: PROTOCOL_VERSION,
      client_name: clientName,
      capabilities: { events: true },
    };
    if (this.storePath !== undefined) {
      initializeParams.store_path = this.storePath;
    }
    if (this.configPath !== undefined) {
      initializeParams.config_path = this.configPath;
    }
    const result = await this.request<InitializeResult>("initialize", initializeParams);
    this.notify("initialized", { protocol_version: PROTOCOL_VERSION });
    this.initializationResult = result;
    this.initialized = true;
    return result;
  }

  public request<TResult = unknown>(
    method: string,
    params: JsonObject = {},
  ): Promise<TResult> {
    if (this.closed) {
      return Promise.reject(new Error("Worker process is closed"));
    }
    const id: JsonRpcId = this.nextId++;
    const request: JsonRpcRequest = { jsonrpc: "2.0", id, method, params };
    return new Promise<TResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(this.idKey(id));
        reject(new Error(`RPC request timed out: ${method}`));
      }, this.requestTimeoutMs);
      this.pending.set(this.idKey(id), {
        resolve: (value) => resolve(value as TResult),
        reject,
        timer,
      });
      try {
        this.write(request as unknown as JsonObject);
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(this.idKey(id));
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  public notify(method: string, params: JsonObject = {}): void {
    if (this.closed) {
      throw new Error("Worker process is closed");
    }
    this.write({ jsonrpc: "2.0", method, params });
  }

  public async createSession(params: SessionCreateParams): Promise<SessionDescriptor> {
    const result = await this.request<{ session: SessionDescriptor }>(
      "session.create",
      params as unknown as JsonObject,
    );
    return result.session;
  }

  public async listSessions(limit = 50): Promise<SessionDescriptor[]> {
    const result = await this.request<{ sessions: SessionDescriptor[] }>("session.list", {
      limit,
    });
    return result.sessions;
  }

  public async getSession(sessionId: string): Promise<SessionDescriptor> {
    const result = await this.request<{ session: SessionDescriptor }>("session.get", {
      session_id: sessionId,
    });
    return result.session;
  }

  public async getSessionEvents(
    sessionId: string,
    afterSeq = 0,
    limit = 100,
  ): Promise<SessionEventsResult> {
    return this.request<SessionEventsResult>("session.events", {
      session_id: sessionId,
      after_seq: afterSeq,
      limit,
    });
  }

  public async createTask(params: TaskCreateParams): Promise<TaskDescriptor> {
    const result = await this.request<{ task: TaskDescriptor }>(
      "task.create",
      params as unknown as JsonObject,
    );
    return result.task;
  }

  public async spawnTask(params: TaskSpawnParams): Promise<TaskDescriptor> {
    const result = await this.request<{ task: TaskDescriptor }>(
      "task.spawn",
      params as unknown as JsonObject,
    );
    return result.task;
  }

  public async getTask(sessionId: string, taskId: string): Promise<TaskDescriptor> {
    const result = await this.request<{ task: TaskDescriptor }>("task.get", {
      session_id: sessionId,
      task_id: taskId,
    });
    return result.task;
  }

  public async listTasks(sessionId: string, activeOnly = false): Promise<TaskDescriptor[]> {
    const result = await this.request<{ tasks: TaskDescriptor[] }>("task.list", {
      session_id: sessionId,
      active_only: activeOnly,
    });
    return result.tasks;
  }

  public async transitionTask(params: TaskTransitionParams): Promise<TaskDescriptor> {
    const result = await this.request<{ task: TaskDescriptor }>(
      "task.transition",
      params as unknown as JsonObject,
    );
    return result.task;
  }

  public async startRun(params: RunStartParams): Promise<RunResult> {
    return this.request<RunResult>("run.start", params as unknown as JsonObject);
  }

  public async resumeRun(params: RunResumeParams): Promise<RunResult> {
    return this.request<RunResult>("run.resume", params as unknown as JsonObject);
  }

  public async listApprovals(
    sessionId: string,
    runId?: string,
  ): Promise<ApprovalDescriptor[]> {
    const result = await this.request<{ approvals: ApprovalDescriptor[] }>("approval.list", {
      session_id: sessionId,
      ...(runId ? { run_id: runId } : {}),
    });
    return result.approvals;
  }

  public async resolveApproval(params: ApprovalResolveParams): Promise<{
    approval_id: string;
    approved: boolean;
    one_shot: boolean;
  }> {
    return this.request("approval.resolve", params as unknown as JsonObject);
  }

  public async listSkills(sessionId: string): Promise<SkillDescriptor[]> {
    const result = await this.request<{ skills: SkillDescriptor[] }>("skill.list", {
      session_id: sessionId,
    });
    return result.skills;
  }

  public async trustSkill(
    sessionId: string,
    name: string,
    enable = true,
  ): Promise<JsonObject> {
    const result = await this.request<{ skill: JsonObject }>("skill.trust", {
      session_id: sessionId,
      name,
      enable,
      trusted_by: "tui",
    });
    return result.skill;
  }

  public async close(): Promise<void> {
    if (this.child.exitCode !== null || this.closed) {
      await this.exitPromise;
      return;
    }
    if (this.initialized) {
      try {
        await this.request("shutdown");
      } catch {
        // The process may already have exited; close remains best effort.
      }
    }
    await Promise.race([
      this.exitPromise,
      new Promise<void>((resolve) => setTimeout(resolve, 1_000)),
    ]);
    if (!this.closed) {
      this.child.kill();
      await this.exitPromise;
    }
  }

  private write(message: JsonObject): void {
    if (!this.child.stdin.writable || this.closed) {
      throw new Error("Worker stdin is not writable");
    }
    this.child.stdin.write(encodeFrame(message));
  }

  private attachProcessListeners(): void {
    this.child.stdout.on("data", (chunk: Buffer) => {
      try {
        for (const message of this.decoder.push(chunk)) {
          this.handleMessage(message);
        }
      } catch (error) {
        this.failPending(
          error instanceof Error ? error : new Error(String(error)),
        );
        this.child.kill();
      }
    });
    this.child.stderr.on("data", (chunk: Buffer) => {
      this.onLogCallback?.(chunk.toString("utf8"));
    });
    this.child.on("error", (error) => {
      this.failPending(error);
    });
    this.child.on("close", (code, signal) => {
      this.closed = true;
      this.failPending(new Error(`Worker exited (code=${code}, signal=${signal ?? "none"})`));
      this.onExitCallback?.(code, signal);
      this.resolveExit();
    });
  }

  private handleMessage(message: JsonObject): void {
    if (message.jsonrpc !== "2.0") {
      throw new Error("Malformed JSON-RPC response: jsonrpc must be '2.0'");
    }
    if (message.method === "event" && !Object.prototype.hasOwnProperty.call(message, "id")) {
      this.handleEvent(message);
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(message, "id")) {
      return;
    }
    const id = message.id;
    if (!this.isRpcId(id)) {
      return;
    }
    const pending = this.pending.get(this.idKey(id));
    if (!pending) {
      return;
    }
    this.pending.delete(this.idKey(id));
    clearTimeout(pending.timer);
    const hasResult = Object.prototype.hasOwnProperty.call(message, "result");
    const hasError = Object.prototype.hasOwnProperty.call(message, "error");
    if (hasResult === hasError) {
      pending.reject(new Error("Malformed JSON-RPC response: expected result or error"));
      return;
    }
    if (hasError) {
      if (!this.isRpcError(message.error)) {
        pending.reject(new Error("Malformed JSON-RPC response: invalid error object"));
        return;
      }
      pending.reject(new RpcError(message.error));
      return;
    }
    pending.resolve(message.result);
  }

  private handleEvent(message: JsonObject): void {
    const notification = message as unknown as EventNotification;
    const params = notification.params;
    if (
      !params ||
      typeof params !== "object" ||
      params.protocol_version !== PROTOCOL_VERSION ||
      !("event" in params)
    ) {
      throw new Error("Malformed event notification");
    }
    const event = (params as { event?: unknown }).event;
    if (!this.isAgentEvent(event)) {
      throw new Error("Malformed event notification payload");
    }
    try {
      this.onEventCallback?.(event);
      for (const listener of this.eventListeners) {
        try {
          listener(event);
        } catch (error) {
          this.onLogCallback?.(
            `aihi-code-cli event listener error: ${error instanceof Error ? error.message : String(error)}\n`,
          );
        }
      }
    } catch (error) {
      this.onLogCallback?.(
        `aihi-code-cli event handler error: ${error instanceof Error ? error.message : String(error)}\n`,
      );
    }
  }

  private failPending(error: Error): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      this.pending.delete(id);
      pending.reject(error);
    }
  }

  private idKey(id: JsonRpcId): string {
    return `${typeof id}:${String(id)}`;
  }

  private isRpcId(value: unknown): value is JsonRpcId {
    return (
      typeof value === "string" ||
      (typeof value === "number" && Number.isSafeInteger(value))
    );
  }

  private isAgentEvent(value: unknown): value is AgentEvent {
    if (typeof value !== "object" || value === null) {
      return false;
    }
    const event = value as Record<string, unknown>;
    return (
      typeof event.session_id === "string" &&
      event.session_id.length > 0 &&
      typeof event.event_type === "string" &&
      event.event_type.length > 0 &&
      typeof event.ephemeral === "boolean" &&
      typeof event.data === "object" &&
      event.data !== null &&
      !Array.isArray(event.data) &&
      (event.seq === undefined ||
        (typeof event.seq === "number" && Number.isSafeInteger(event.seq) && event.seq > 0))
    );
  }

  private isRpcError(value: unknown): value is JsonRpcError {
    return (
      typeof value === "object" &&
      value !== null &&
      typeof (value as { code?: unknown }).code === "number" &&
      typeof (value as { message?: unknown }).message === "string"
    );
  }
}
