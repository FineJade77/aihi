import type { ChildProcessWithoutNullStreams } from "node:child_process";
import type {
  AgentEvent,
  ApprovalDescriptor,
  ApprovalResolveParams,
  CodeRpcMethod,
  CodeRpcParams,
  CodeRpcResult,
  ConfigDescriptor,
  HostAcknowledgement,
  InitializeParams,
  InitializeResult,
  JsonObject,
  JsonRpcError,
  JsonRpcId,
  JsonRpcRequest,
  McpServerDescriptor,
  RunAccepted,
  RunCancelParams,
  RunCancelResult,
  RunDescriptor,
  RunError,
  RunResumeParams,
  RunStartParams,
  SessionCreateParams,
  SessionDescriptor,
  SessionEventsResult,
  SessionUsage,
  SkillDescriptor,
  TaskCreateParams,
  TaskDescriptor,
  TaskSpawnParams,
  TaskTransitionParams,
  ToolDescriptor,
} from "@aihi/code-protocol";
import {
  PROTOCOL_VERSION,
  isApprovalDescriptor,
  isApprovalResolution,
  isConfigDescriptor,
  isEventNotification,
  isInitializeResult,
  isRunAccepted,
  isRunDescriptor,
  isRunErrorNotification,
  isSessionDescriptor,
  isSessionEventsResult,
  isTaskDescriptor,
} from "@aihi/code-protocol";
import { ContentLengthDecoder, encodeFrame } from "./framing.js";
import { launchWorker, type WorkerLaunchOptions } from "../worker/launcher.js";

export interface RpcClientOptions extends WorkerLaunchOptions {
  requestTimeoutMs?: number;
  storePath?: string;
  onEvent?: (event: AgentEvent) => void;
  onRunError?: (error: RunError) => void;
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

export type {
  ApprovalResolveParams,
  RunCancelParams,
  RunResumeParams,
  RunStartParams,
  SessionCreateParams,
  TaskCreateParams,
  TaskSpawnParams,
  TaskTransitionParams,
} from "@aihi/code-protocol";

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
  private readonly onEventCallback?: (event: AgentEvent) => void;
  private readonly onRunErrorCallback?: (error: RunError) => void;
  private readonly onLogCallback?: (chunk: string) => void;
  private readonly onExitCallback?: (code: number | null, signal: NodeJS.Signals | null) => void;
  private readonly eventListeners = new Set<(event: AgentEvent) => void>();
  private readonly runErrorListeners = new Set<(error: RunError) => void>();
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
    this.onEventCallback = options.onEvent;
    this.onRunErrorCallback = options.onRunError;
    this.onLogCallback = options.onLog;
    this.onExitCallback = options.onExit;
    this.exitPromise = new Promise<void>((resolve) => {
      this.resolveExit = resolve;
    });
    this.attachProcessListeners();
  }

  public static async connect(options: RpcClientOptions = {}): Promise<RpcClient> {
    const launchOptions: RpcClientOptions = options.storePath === undefined
      ? options
      : {
          ...options,
          env: { ...options.env, AIHI_CODE_AGENT_STORE: options.storePath },
        };
    const client = new RpcClient(launchWorker(launchOptions), options);
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

  /** Subscribe to runs that failed before producing any terminal event. */
  public subscribeRunErrors(
    listener: (error: RunError) => void,
  ): () => void {
    this.runErrorListeners.add(listener);
    return () => this.runErrorListeners.delete(listener);
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
    const initializeParams: InitializeParams = {
      protocol_version: PROTOCOL_VERSION,
      client_name: clientName,
      capabilities: { events: true },
    };
    if (this.storePath !== undefined) {
      initializeParams.store_path = this.storePath;
    }
    const result = await this.request("initialize", initializeParams);
    if (!isInitializeResult(result)) throw new Error("Malformed initialize result");
    this.notify("initialized", { protocol_version: PROTOCOL_VERSION });
    this.initializationResult = result;
    this.initialized = true;
    return result;
  }

  public request<TMethod extends CodeRpcMethod>(
    method: TMethod,
    params: CodeRpcParams<TMethod>,
  ): Promise<CodeRpcResult<TMethod>> {
    if (this.closed) {
      return Promise.reject(new Error("Worker process is closed"));
    }
    const id: JsonRpcId = this.nextId++;
    const request: JsonRpcRequest<CodeRpcParams<TMethod>> = {
      jsonrpc: "2.0",
      id,
      method,
      params,
    };
    return new Promise<CodeRpcResult<TMethod>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(this.idKey(id));
        reject(new Error(`RPC request timed out: ${method}`));
      }, this.requestTimeoutMs);
      this.pending.set(this.idKey(id), {
        resolve: (value) => resolve(value as CodeRpcResult<TMethod>),
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
    const result = await this.request("session.create", params);
    if (!isSessionDescriptor(result.session)) throw new Error("Malformed session.create result");
    return result.session;
  }

  public async listSessions(limit = 50): Promise<SessionDescriptor[]> {
    const result = await this.request("session.list", {
      limit,
    });
    if (!Array.isArray(result.sessions) || !result.sessions.every(isSessionDescriptor)) {
      throw new Error("Malformed session.list result");
    }
    return result.sessions;
  }

  public async getSession(sessionId: string): Promise<SessionDescriptor> {
    const result = await this.request("session.get", {
      session_id: sessionId,
    });
    if (!isSessionDescriptor(result.session)) throw new Error("Malformed session.get result");
    return result.session;
  }

  public async getSessionEvents(
    sessionId: string,
    afterSeq = 0,
    limit = 100,
  ): Promise<SessionEventsResult> {
    const result = await this.request("session.events", {
      session_id: sessionId,
      after_seq: afterSeq,
      limit,
    });
    if (!isSessionEventsResult(result)) throw new Error("Malformed session.events result");
    return result;
  }

  /** Totals this session's spend and how full its context last was. */
  public async getSessionUsage(sessionId: string): Promise<SessionUsage> {
    return this.request("session.usage", { session_id: sessionId });
  }

  public async createTask(params: TaskCreateParams): Promise<TaskDescriptor> {
    const result = await this.request("task.create", params);
    if (!isTaskDescriptor(result.task)) throw new Error("Malformed task.create result");
    return result.task;
  }

  public async spawnTask(params: TaskSpawnParams): Promise<TaskDescriptor> {
    const result = await this.request("task.spawn", params);
    if (!isTaskDescriptor(result.task)) throw new Error("Malformed task.spawn result");
    return result.task;
  }

  public async getTask(sessionId: string, taskId: string): Promise<TaskDescriptor> {
    const result = await this.request("task.get", {
      session_id: sessionId,
      task_id: taskId,
    });
    if (!isTaskDescriptor(result.task)) throw new Error("Malformed task.get result");
    return result.task;
  }

  public async listTasks(sessionId: string, activeOnly = false): Promise<TaskDescriptor[]> {
    const result = await this.request("task.list", {
      session_id: sessionId,
      active_only: activeOnly,
    });
    if (!Array.isArray(result.tasks) || !result.tasks.every(isTaskDescriptor)) {
      throw new Error("Malformed task.list result");
    }
    return result.tasks;
  }

  public async transitionTask(params: TaskTransitionParams): Promise<TaskDescriptor> {
    const result = await this.request("task.transition", params);
    if (!isTaskDescriptor(result.task)) throw new Error("Malformed task.transition result");
    return result.task;
  }

  /** Returns as soon as the Worker accepts the run; watch events for the outcome. */
  public async startRun(params: RunStartParams): Promise<RunAccepted> {
    const result = await this.request("run.start", params);
    if (!isRunAccepted(result)) throw new Error("Malformed run.start acknowledgement");
    return result;
  }

  /** Seeds ~/.aihi/aihi-code.toml when absent; never overwrites an existing file. */
  public async initConfig(): Promise<{ path: string; created: boolean }> {
    return this.request("config.init", {});
  }

  public async acknowledgeHost(cwd: string): Promise<HostAcknowledgement> {
    return this.request("config.acknowledge_host", { cwd, acknowledged: true });
  }

  public async getConfig(cwd?: string): Promise<ConfigDescriptor> {
    const result = await this.request("config.get", {
      ...(cwd ? { cwd } : {}),
    });
    if (!isConfigDescriptor(result.config)) throw new Error("Malformed config.get result");
    return result.config;
  }

  /** Returns as soon as the Worker accepts the resume; watch events for the outcome. */
  public async resumeRun(params: RunResumeParams): Promise<RunAccepted> {
    const result = await this.request("run.resume", params);
    if (!isRunAccepted(result)) throw new Error("Malformed run.resume acknowledgement");
    return result;
  }

  public async listRuns(sessionId: string): Promise<RunDescriptor[]> {
    const result = await this.request("run.list", {
      session_id: sessionId,
    });
    if (!Array.isArray(result.runs) || !result.runs.every(isRunDescriptor)) {
      throw new Error("Malformed run.list result");
    }
    return result.runs;
  }

  public async cancelRun(params: RunCancelParams): Promise<RunCancelResult> {
    return this.request("run.cancel", params);
  }

  public async forkSession(sessionId: string, atSeq?: number): Promise<SessionDescriptor> {
    const result = await this.request("session.fork", {
      session_id: sessionId,
      ...(atSeq !== undefined ? { at_seq: atSeq } : {}),
    });
    if (!isSessionDescriptor(result.session)) throw new Error("Malformed session.fork result");
    return result.session;
  }

  public async listApprovals(
    sessionId: string,
    runId?: string,
  ): Promise<ApprovalDescriptor[]> {
    const result = await this.request("approval.list", {
      session_id: sessionId,
      ...(runId ? { run_id: runId } : {}),
    });
    if (!Array.isArray(result.approvals) || !result.approvals.every(isApprovalDescriptor)) {
      throw new Error("Malformed approval.list result");
    }
    return result.approvals;
  }

  public async resolveApproval(params: ApprovalResolveParams) {
    const result = await this.request("approval.resolve", params);
    if (!isApprovalResolution(result)) throw new Error("Malformed approval.resolve result");
    return result;
  }

  public async listSkills(sessionId: string): Promise<SkillDescriptor[]> {
    const result = await this.request("skill.list", {
      session_id: sessionId,
    });
    return result.skills;
  }

  public async trustSkill(
    sessionId: string,
    name: string,
    enable = true,
  ): Promise<JsonObject> {
    const result = await this.request("skill.trust", {
      session_id: sessionId,
      name,
      enable,
      trusted_by: "tui",
    });
    return result.skill;
  }

  public async untrustSkill(sessionId: string, name: string): Promise<boolean> {
    const result = await this.request("skill.untrust", {
      session_id: sessionId,
      name,
    });
    return result.removed;
  }

  public async listMcpServers(sessionId: string): Promise<McpServerDescriptor[]> {
    const result = await this.request("mcp.list", {
      session_id: sessionId,
    });
    return result.servers;
  }

  public async listTools(sessionId: string): Promise<ToolDescriptor[]> {
    const result = await this.request("tool.list", {
      session_id: sessionId,
    });
    return result.tools;
  }

  public async close(): Promise<void> {
    if (this.child.exitCode !== null || this.closed) {
      await this.exitPromise;
      return;
    }
    if (this.initialized) {
      try {
        await this.request("shutdown", {});
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
      if (!isEventNotification(message)) throw new Error("Malformed event notification");
      this.handleEvent(message.params.event);
      return;
    }
    // A run acknowledged but never started reaches no terminal event, so this
    // notification is its only report.
    if (message.method === "run.error" && !Object.prototype.hasOwnProperty.call(message, "id")) {
      if (!isRunErrorNotification(message)) throw new Error("Malformed run.error notification");
      const error: RunError = {
        session_id: message.params.session_id,
        run_id: message.params.run_id,
        message: message.params.message,
      };
      this.handleRunError(error);
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

  private handleEvent(event: AgentEvent): void {
    try {
      this.onEventCallback?.(event);
    } catch (error) {
      this.onLogCallback?.(
        `aihi-code-cli event handler error: ${error instanceof Error ? error.message : String(error)}\n`,
      );
    }
    for (const listener of this.eventListeners) {
      try {
        listener(event);
      } catch (error) {
        this.onLogCallback?.(
          `aihi-code-cli event listener error: ${error instanceof Error ? error.message : String(error)}\n`,
        );
      }
    }
  }

  private handleRunError(error: RunError): void {
    try {
      this.onRunErrorCallback?.(error);
    } catch (callbackError) {
      this.onLogCallback?.(
        `aihi-code-cli run error handler failed: ${callbackError instanceof Error ? callbackError.message : String(callbackError)}\n`,
      );
    }
    for (const listener of this.runErrorListeners) {
      try {
        listener(error);
      } catch (listenerError) {
        this.onLogCallback?.(
          `aihi-code-cli run error listener failed: ${listenerError instanceof Error ? listenerError.message : String(listenerError)}\n`,
        );
      }
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

  private isRpcError(value: unknown): value is JsonRpcError {
    return (
      typeof value === "object" &&
      value !== null &&
      typeof (value as { code?: unknown }).code === "number" &&
      Number.isSafeInteger((value as { code: number }).code) &&
      typeof (value as { message?: unknown }).message === "string"
    );
  }
}
