import type { AgentEvent, EventRecord, JsonObject } from "@aihi/code-protocol";

export type TranscriptToolStatus =
  | "proposed"
  | "requested"
  | "waiting_approval"
  | "approved"
  | "running"
  | "succeeded"
  | "failed"
  | "rejected"
  | "denied";

export interface TranscriptEntry {
  id: string;
  kind: "user" | "assistant" | "tool" | "status";
  seq: number;
  updatedSeq: number;
  runId?: string;
  text: string;
  detail?: string;
  toolCallId?: string;
  toolName?: string;
  approvalId?: string;
  status?: TranscriptToolStatus;
  isError?: boolean;
}

export interface TranscriptProjection {
  sessionId?: string;
  headSeq: number;
  activeRunId?: string;
  entries: TranscriptEntry[];
}

export interface TranscriptEvent {
  sessionId: string;
  runId?: string;
  seq: number | null;
  type: string;
  ephemeral: boolean;
  data: JsonObject;
}

export class TranscriptGapError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "TranscriptGapError";
  }
}

export function transcriptEventFromRecord(event: EventRecord): TranscriptEvent {
  return {
    sessionId: event.session_id,
    ...(event.run_id === null ? {} : { runId: event.run_id }),
    seq: event.seq,
    type: event.type,
    ephemeral: event.ephemeral,
    data: event.data,
  };
}

export function transcriptEventFromNotification(event: AgentEvent): TranscriptEvent {
  return {
    sessionId: event.session_id,
    ...(event.run_id === undefined ? {} : { runId: event.run_id }),
    seq: event.seq ?? null,
    type: event.event_type,
    ephemeral: event.ephemeral,
    data: event.data,
  };
}

export function projectTranscript(events: EventRecord[]): TranscriptProjection {
  let projection: TranscriptProjection = { headSeq: 0, entries: [] };
  for (const event of events) {
    projection = appendTranscriptEvent(projection, transcriptEventFromRecord(event));
  }
  return projection;
}

/** Merge notifications captured while replay was in flight, regardless of arrival order. */
export function mergeTranscriptEvents(
  projection: TranscriptProjection,
  events: readonly TranscriptEvent[],
): TranscriptProjection {
  return [...events]
    .sort((left, right) => (left.seq ?? 0) - (right.seq ?? 0))
    .reduce(appendTranscriptEvent, projection);
}

/** Apply the same durable event reducer for initial replay and live notifications. */
export function appendTranscriptEvent(
  projection: TranscriptProjection,
  event: TranscriptEvent,
): TranscriptProjection {
  if (event.ephemeral) return projection;
  if (event.seq === null || !Number.isSafeInteger(event.seq) || event.seq < 1) {
    throw new TranscriptGapError("Durable transcript event is missing a valid sequence");
  }
  if (projection.sessionId !== undefined && projection.sessionId !== event.sessionId) {
    throw new TranscriptGapError(
      `Transcript event belongs to ${event.sessionId}, expected ${projection.sessionId}`,
    );
  }
  if (event.seq <= projection.headSeq) return projection;
  if (event.seq !== projection.headSeq + 1) {
    throw new TranscriptGapError(
      `Transcript event sequence jumped from ${projection.headSeq} to ${event.seq}`,
    );
  }
  const applied = applyEvent(projection, event, event.seq);
  return {
    ...applied,
    sessionId: event.sessionId,
    headSeq: event.seq,
  };
}

function applyEvent(
  projection: TranscriptProjection,
  event: TranscriptEvent,
  seq: number,
): TranscriptProjection {
  if (["message.added", "user.message", "assistant.message"].includes(event.type)) {
    return applyMessage(projection, event, seq);
  }
  if (event.type === "run.started" || event.type === "run.resumed") {
    return event.runId === undefined ? projection : { ...projection, activeRunId: event.runId };
  }
  if (event.type === "tool.requested") {
    const callId = text(event.data.tool_call_id);
    if (callId === undefined) return projection;
    const toolName = text(event.data.tool_name);
    return upsertTool(projection, event, seq, callId, {
      ...(toolName === undefined ? {} : { toolName }),
      ...(toolName === undefined
        ? {}
        : { text: toolPreview(toolName, object(event.data.input)) }),
      status: "requested",
    });
  }
  if (event.type === "tool.started") {
    return updateToolLifecycle(projection, event, seq, "running");
  }
  if (event.type === "tool.completed") {
    return updateToolLifecycle(
      projection,
      event,
      seq,
      event.data.is_error === true ? "failed" : "succeeded",
      event.data.is_error === true,
    );
  }
  if (event.type === "tool.rejected") {
    return updateToolLifecycle(projection, event, seq, "rejected", true, text(event.data.error_code));
  }
  if (event.type === "tool.result") {
    return applyToolResults(projection, event, seq);
  }
  if (event.type === "approval.requested") {
    return applyApprovalRequested(projection, event, seq);
  }
  if (event.type === "approval.resolved") {
    return applyApprovalResolved(projection, event, seq);
  }
  if (event.type === "run.completed") {
    return event.runId === projection.activeRunId
      ? { ...projection, activeRunId: undefined }
      : projection;
  }
  if (["run.failed", "run.interrupted", "run.cancelled"].includes(event.type)) {
    const state = event.type.slice("run.".length);
    const detail = text(event.data.error) ?? text(event.data.reason);
    const withStatus = appendEntry(projection, {
      id: `run:${event.runId ?? seq}:${event.type}:${seq}`,
      kind: "status",
      seq,
      updatedSeq: seq,
      ...(event.runId === undefined ? {} : { runId: event.runId }),
      text: `Run ${state}`,
      ...(detail === undefined ? {} : { detail: bounded(detail, 800) }),
      isError: event.type === "run.failed" || event.type === "run.interrupted",
    });
    return event.runId === projection.activeRunId
      ? { ...withStatus, activeRunId: undefined }
      : withStatus;
  }
  return projection;
}

function applyMessage(
  projection: TranscriptProjection,
  event: TranscriptEvent,
  seq: number,
): TranscriptProjection {
  const message = object(event.data.message);
  if (message === undefined) return projection;
  const messageId = text(message.id) ?? `seq_${seq}`;
  const content = Array.isArray(message.content) ? message.content : [];
  const messageRole = text(message.role);
  if (messageRole === "system") return projection;
  const role = event.type === "user.message" || messageRole === "user"
    ? "user"
    : "assistant";
  const renderedText = content
    .map((raw) => {
      const part = object(raw);
      return part?.kind === "text" && typeof part.text === "string" ? part.text : "";
    })
    .join("");
  let next = projection;
  if (renderedText.trim()) {
    next = appendEntry(next, {
      id: `message:${messageId}:text`,
      kind: role,
      seq,
      updatedSeq: seq,
      ...(event.runId === undefined ? {} : { runId: event.runId }),
      text: bounded(renderedText, 4_000),
    });
  }
  if (role !== "assistant") return next;
  for (const raw of content) {
    const part = object(raw);
    if (part?.kind !== "tool_call") continue;
    const callId = text(part.id);
    if (callId === undefined) continue;
    const toolName = text(part.name) ?? "tool";
    next = upsertTool(next, event, seq, callId, {
      toolName,
      text: toolPreview(toolName, object(part.input)),
      status: "proposed",
    });
  }
  return next;
}

function applyToolResults(
  projection: TranscriptProjection,
  event: TranscriptEvent,
  seq: number,
): TranscriptProjection {
  const message = object(event.data.message);
  const content = Array.isArray(message?.content) ? message.content : [];
  let next = projection;
  for (const raw of content) {
    const part = object(raw);
    if (part?.kind !== "tool_result") continue;
    const callId = text(part.tool_call_id) ?? text(part.tool_use_id);
    if (callId === undefined) continue;
    const metadata = object(part.metadata);
    const toolName = text(metadata?.tool_name);
    const isError = part.is_error === true;
    next = upsertTool(next, event, seq, callId, {
      ...(toolName === undefined ? {} : { toolName }),
      status: isError ? "failed" : "succeeded",
      isError,
      detail: bounded(text(part.content) ?? "", 1_200),
    });
  }
  return next;
}

function applyApprovalRequested(
  projection: TranscriptProjection,
  event: TranscriptEvent,
  seq: number,
): TranscriptProjection {
  const approval = object(event.data.approval);
  const approvalId = text(event.data.approval_id) ?? text(approval?.approval_id);
  const callId = text(event.data.tool_call_id);
  if (approvalId === undefined || callId === undefined) return projection;
  const toolName = text(event.data.tool_name) ?? text(approval?.scope) ?? "tool";
  return upsertTool(projection, event, seq, callId, {
    toolName,
    approvalId,
    status: "waiting_approval",
    detail: bounded(text(event.data.reason) ?? "Approval required", 800),
  });
}

function applyApprovalResolved(
  projection: TranscriptProjection,
  event: TranscriptEvent,
  seq: number,
): TranscriptProjection {
  const approvalId = text(event.data.approval_id);
  if (approvalId === undefined) return projection;
  const status = event.data.status === "denied" ? "denied" : "approved";
  return updateEntry(projection, (entry) => entry.approvalId === approvalId, (entry) => ({
    ...entry,
    status,
    isError: status === "denied",
    updatedSeq: seq,
  }));
}

function updateToolLifecycle(
  projection: TranscriptProjection,
  event: TranscriptEvent,
  seq: number,
  status: TranscriptToolStatus,
  isError = false,
  detail?: string,
): TranscriptProjection {
  const callId = text(event.data.tool_call_id);
  if (callId === undefined) return projection;
  const toolName = text(event.data.tool_name);
  return upsertTool(projection, event, seq, callId, {
    ...(toolName === undefined ? {} : { toolName }),
    status,
    isError,
    ...(detail === undefined ? {} : { detail: bounded(detail, 800) }),
  });
}

function upsertTool(
  projection: TranscriptProjection,
  event: TranscriptEvent,
  seq: number,
  callId: string,
  patch: Partial<TranscriptEntry> & { status: TranscriptToolStatus },
): TranscriptProjection {
  const index = projection.entries.findIndex((entry) => entry.toolCallId === callId);
  if (index < 0) {
    const toolName = patch.toolName ?? "tool";
    return appendEntry(projection, {
      id: `tool:${callId}`,
      kind: "tool",
      seq,
      updatedSeq: seq,
      ...(event.runId === undefined ? {} : { runId: event.runId }),
      text: patch.text ?? toolName,
      toolCallId: callId,
      toolName,
      ...patch,
    });
  }
  return updateEntry(projection, (_, candidateIndex) => candidateIndex === index, (entry) => ({
    ...entry,
    ...patch,
    text: patch.text ?? entry.text,
    toolName: patch.toolName ?? entry.toolName,
    status: patch.status === "failed" && ["denied", "rejected"].includes(entry.status ?? "")
      ? entry.status
      : patch.status,
    updatedSeq: seq,
  }));
}

function appendEntry(
  projection: TranscriptProjection,
  entry: TranscriptEntry,
): TranscriptProjection {
  return { ...projection, entries: [...projection.entries, entry] };
}

function updateEntry(
  projection: TranscriptProjection,
  predicate: (entry: TranscriptEntry, index: number) => boolean,
  update: (entry: TranscriptEntry) => TranscriptEntry,
): TranscriptProjection {
  const index = projection.entries.findIndex(predicate);
  if (index < 0) return projection;
  const entries = [...projection.entries];
  entries[index] = update(entries[index]);
  return { ...projection, entries };
}

/** Render a redacted summary only; arbitrary Tool inputs may contain credentials. */
function toolPreview(toolName: string, input: JsonObject | undefined): string {
  if (input === undefined) return toolName;
  if (toolName === "bash" && typeof input.command === "string") {
    return `$ ${bounded(redactPreview(input.command), 500)}`;
  }
  if (typeof input.path === "string") return `${toolName} · ${bounded(redactPreview(input.path), 500)}`;
  if (typeof input.pattern === "string") return `${toolName} · ${bounded(redactPreview(input.pattern), 500)}`;
  if (typeof input.query === "string") return `${toolName} · ${bounded(redactPreview(input.query), 500)}`;
  if (typeof input.objective === "string") return `${toolName} · ${bounded(redactPreview(input.objective), 500)}`;
  if (typeof input.name === "string") return `${toolName} · ${bounded(redactPreview(input.name), 500)}`;
  return toolName;
}

function redactPreview(value: string): string {
  return value
    .replace(
      /\b(authorization)\b\s*[:=]\s*(?:bearer\s+)?(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      "$1=[REDACTED]",
    )
    .replace(
      /\b([a-z0-9_]*(?:api[-_]?key|token|password|secret)[a-z0-9_]*)\b(?:\s*[:=]\s*|\s+)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      "$1=[REDACTED]",
    )
    .replace(
      /\b(bearer)\s+(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      "$1 [REDACTED]",
    );
}

function object(value: unknown): JsonObject | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as JsonObject
    : undefined;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function bounded(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}
