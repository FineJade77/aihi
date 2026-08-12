import { homedir } from "node:os";
import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  AgentEvent,
  ApprovalDescriptor,
  EventRecord,
  JsonObject,
  RunResult,
  SessionDescriptor,
  TaskDescriptor,
} from "@aihi/code-protocol";
import { RpcClient } from "../rpc/client.js";
import { Banner, GradientText } from "./banner.js";

const COLORS = {
  brand: "cyan",
  accent: "magenta",
  muted: "gray",
  good: "green",
  warn: "yellow",
  bad: "red",
  panel: "blue",
} as const;
type UiColor = (typeof COLORS)[keyof typeof COLORS];

interface UiEvent {
  seq: number | null;
  type: string;
  ephemeral: boolean;
  data: JsonObject;
}

export interface TuiAppProps {
  client: RpcClient;
  cwd: string;
  provider: string;
  model: string;
  sessionId?: string;
  storePath?: string;
  configPaths?: string[];
  /** First turn to run once the session is up. */
  prompt?: string;
  onSessionOpened?: (sessionId: string) => void;
}

function eventFromRecord(event: EventRecord): UiEvent {
  return {
    seq: event.seq,
    type: event.type,
    ephemeral: event.ephemeral,
    data: event.data,
  };
}

function eventFromNotification(event: AgentEvent): UiEvent {
  return {
    seq: event.seq ?? null,
    type: event.event_type,
    ephemeral: event.ephemeral,
    data: event.data,
  };
}

/** Joins the text parts of a Message payload; other content kinds are skipped. */
function messageText(data: JsonObject): string {
  const message = data.message;
  if (typeof message !== "object" || message === null || Array.isArray(message)) return "";
  const content = (message as { content?: unknown }).content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (typeof part !== "object" || part === null) return "";
      const { kind, text } = part as { kind?: unknown; text?: unknown };
      return kind === "text" && typeof text === "string" ? text : "";
    })
    .join("");
}


function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}


function shortId(value: string): string {
  return value.length > 16 ? `…${value.slice(-14)}` : value;
}

/** Abbreviates the user's home prefix so startup paths stay readable. */
function tildePath(value: string): string {
  const home = homedir();
  if (home && (value === home || value.startsWith(`${home}/`))) {
    return `~${value.slice(home.length)}`;
  }
  return value;
}

function runStatus(result: RunResult): string {
  if (result.suspended) {
    return `Run ${shortId(result.run_id)} waiting for approval${result.pending_approval_id ? ` · ${shortId(result.pending_approval_id)}` : ""}`;
  }
  if (result.error) return `Run ${shortId(result.run_id)} ${result.state}: ${result.error}`;
  const text = result.response?.message?.content;
  if (Array.isArray(text)) {
    const firstText = text.find(
      (block): block is { kind?: unknown; text?: unknown } =>
        Boolean(block) && typeof block === "object" && "text" in block,
    );
    if (typeof firstText?.text === "string" && firstText.text.trim()) {
      return firstText.text.trim().replace(/\s+/g, " ").slice(0, 120);
    }
  }
  return `Run ${shortId(result.run_id)} ${result.state}`;
}

function approvalStatus(approvals: ApprovalDescriptor[]): string {
  if (approvals.length === 0) return "No pending approvals";
  return approvals
    .slice(0, 3)
    .map((approval) => `${shortId(approval.approval_id)} ${approval.tool_name ?? approval.scope}`)
    .join(" · ");
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      {/* A fixed, non-shrinking label column: a long value would otherwise make
          Yoga shrink this cell and swallow the padding that aligns the rows. */}
      <Box width={10} flexShrink={0}>
        <Text color={COLORS.muted}>{label}</Text>
      </Box>
      <Text wrap="truncate-start">{value}</Text>
    </Box>
  );
}

function Splash({
  cwd,
  provider,
  model,
  storePath,
  configPaths,
}: {
  cwd: string;
  provider: string;
  model: string;
  storePath?: string;
  configPaths?: string[];
}) {
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Banner />
      <Box flexDirection="column" marginTop={1} paddingX={2}>
        <InfoRow label="cwd" value={tildePath(cwd)} />
        <InfoRow label="provider" value={`${provider} · ${model}`} />
        <InfoRow label="store" value={storePath ? tildePath(storePath) : "in-memory (this process only)"} />
        {configPaths !== undefined && configPaths.length > 0 && (
          <InfoRow label="config" value={configPaths.map(tildePath).join(" → ")} />
        )}
      </Box>
    </Box>
  );
}



/** 122775 -> "122.8K": a status bar has no room for exact token counts. */
function compactCount(value: number): string {
  if (value < 1_000) return String(value);
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1)}K`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

/** The bottom bar: where you are, what is answering, and how full the context is. */
function StatusBar({
  cwd,
  provider,
  model,
  contextTokens,
  contextLimit,
  tasks,
}: {
  cwd: string;
  provider: string;
  model: string;
  contextTokens: number;
  contextLimit: number;
  tasks: TaskDescriptor[];
}) {
  const ratio = contextLimit > 0 ? contextTokens / contextLimit : 0;
  const contextTone =
    ratio >= 0.9 ? COLORS.bad : ratio >= 0.7 ? COLORS.warn : COLORS.muted;
  const active = tasks.filter((task) => task.state === "running").length;
  return (
    <Box borderStyle="round" borderColor={COLORS.panel} paddingX={1}>
      <Text color={COLORS.muted} wrap="truncate-start">
        {tildePath(cwd)}
      </Text>
      <Text color={COLORS.muted}>{"  ·  "}</Text>
      <Text color={COLORS.brand}>
        {provider}/{model}
      </Text>
      <Text color={COLORS.muted}>{"  ·  "}</Text>
      {contextLimit > 0 ? (
        <Text color={contextTone}>
          ctx {compactCount(contextTokens)}/{compactCount(contextLimit)}{" "}
          {(ratio * 100).toFixed(1)}%
        </Text>
      ) : (
        <Text color={COLORS.muted}>ctx —</Text>
      )}
      {/* The task graph earns bar space only once there is a graph to show. */}
      {tasks.length > 0 && (
        <>
          <Text color={COLORS.muted}>{"  ·  "}</Text>
          <Text color={active > 0 ? COLORS.warn : COLORS.good}>
            tasks {tasks.length}
            {active > 0 ? ` (${active} running)` : ""}
          </Text>
        </>
      )}
    </Box>
  );
}

/** Pending approvals, with the ids the /approve and /deny commands need. */
function ApprovalPanel({ approvals }: { approvals: ApprovalDescriptor[] }) {
  return (
    <Box borderStyle="round" borderColor={COLORS.warn} flexDirection="column" paddingX={1}>
      <Text bold color={COLORS.warn}>
        APPROVAL REQUIRED ({approvals.length})
      </Text>
      {approvals.slice(0, 5).map((approval) => (
        <Text key={approval.approval_id} wrap="truncate">
          <Text color={COLORS.accent}>{approval.approval_id}</Text>{" "}
          <Text>{approval.tool_name ?? approval.scope}</Text>
        </Text>
      ))}
      <Text color={COLORS.muted}>/approve ID [once] · /deny ID · then /resume RUN_ID</Text>
    </Box>
  );
}

/** The readable answer. The event log truncates; this is where text is legible. */
function AnswerPanel({ text, streaming }: { text: string; streaming: boolean }) {
  const shown = text.length > 1_200 ? `…${text.slice(-1_200)}` : text;
  return (
    <Box
      borderStyle="round"
      borderColor={streaming ? COLORS.accent : COLORS.panel}
      flexDirection="column"
      paddingX={1}
    >
      <Text bold color={COLORS.brand}>
        ANSWER{streaming ? " · streaming" : ""}
      </Text>
      {shown ? <Text wrap="wrap">{shown}</Text> : <Text color={COLORS.muted}>No answer yet</Text>}
    </Box>
  );
}


export function TuiApp({
  client,
  cwd,
  provider,
  model,
  sessionId,
  storePath,
  configPaths,
  prompt,
  onSessionOpened,
}: TuiAppProps) {
  const { exit } = useApp();
  const [sessions, setSessions] = useState<SessionDescriptor[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | undefined>(sessionId);
  const [selectedSession, setSelectedSession] = useState<SessionDescriptor>();
  const [tasks, setTasks] = useState<TaskDescriptor[]>([]);
  const [command, setCommand] = useState("");
  const [status, setStatus] = useState("Connecting to Worker…");
  const [busy, setBusy] = useState(false);
  const [activeProvider, setActiveProvider] = useState(provider);
  const [activeModel, setActiveModel] = useState(model);
  const [activeRunId, setActiveRunId] = useState<string>();
  const [streamText, setStreamText] = useState("");
  const [answerText, setAnswerText] = useState("");
  const [splashVisible, setSplashVisible] = useState(true);
  const [headSeq, setHeadSeq] = useState<number>();
  const [context, setContext] = useState({ tokens: 0, limit: 0 });
  const [promptSent, setPromptSent] = useState(false);
  const [approvals, setApprovals] = useState<ApprovalDescriptor[]>([]);
  // Kept apart from `status`: a notice must survive the event traffic that
  // follows it, which is exactly what the old status heartbeat destroyed.
  const [notice, setNotice] = useState<{ text: string; tone: UiColor }>();

  const loadSession = useCallback(async (sessionId: string) => {
    const [session, page, nextTasks, nextApprovals] = await Promise.all([
      client.getSession(sessionId),
      client.getSessionEvents(sessionId, 0, 100),
      client.listTasks(sessionId),
      client.listApprovals(sessionId),
    ]);
    setApprovals(nextApprovals);
    setHeadSeq(page.head_seq);
    setSelectedSessionId(session.session_id);
    setSelectedSession(session);
    const history = page.events.map(eventFromRecord);
    setTasks(nextTasks);
    setStreamText("");
    // Reopening a session should still show its last answer, not an empty pane.
    const lastAnswer = [...history].reverse().find((item) => item.type === "assistant.message");
    setAnswerText(lastAnswer ? messageText(lastAnswer.data) : "");
    setStatus(`Session ${shortId(session.session_id)} · seq ${page.head_seq}`);
  }, [client]);

  const refreshSessions = useCallback(async () => {
    const next = await client.listSessions();
    setSessions(next);
    const target = selectedSessionId && next.some((item) => item.session_id === selectedSessionId)
      ? selectedSessionId
      : next[0]?.session_id;
    if (target) {
      await loadSession(target);
    } else {
      setSelectedSession(undefined);
      setTasks([]);
      setStatus("No session yet · use /new to create one");
    }
  }, [client, loadSession, selectedSessionId]);

  useEffect(() => {
    const bootstrap = async () => {
      if (!sessionId) throw new Error("No bootstrapped session was provided");
      await loadSession(sessionId);
      await client.listSessions().then(setSessions).catch(() => undefined);
    };
    void bootstrap().catch((error) => setStatus(`Load failed: ${errorMessage(error)}`));
  }, [client, loadSession, sessionId]);

  useEffect(() => {
    if (selectedSessionId) onSessionOpened?.(selectedSessionId);
  }, [selectedSessionId, onSessionOpened]);

  useEffect(() => {
    return client.subscribeRunErrors((runId, message) => {
      setActiveRunId(undefined);
      setNotice({
        text: `Run ${runId ? shortId(runId) : ""} failed to start: ${message}`,
        tone: COLORS.bad,
      });
    });
  }, [client]);

  useEffect(() => {
    const unsubscribe = client.subscribeEvents((event) => {
      if (event.session_id !== selectedSessionId) return;
      const nextEvent = eventFromNotification(event);
      if (nextEvent.type === "run.started" || nextEvent.type === "run.resumed") {
        setActiveRunId(event.run_id);
        setStreamText("");
        setAnswerText("");
        setNotice(undefined);
      }
      if (nextEvent.type === "model.chunk" && nextEvent.data.kind === "text_delta") {
        const delta = nextEvent.data.text;
        if (typeof delta === "string") setStreamText((current) => `${current}${delta}`);
      }
      if (nextEvent.type === "assistant.message") {
        // The persisted message is authoritative; drop the accumulated deltas.
        setAnswerText(messageText(nextEvent.data));
        setStreamText("");
      }
      if (["run.completed", "run.failed", "run.interrupted", "run.cancelled"].includes(nextEvent.type)) {
        setActiveRunId(undefined);
        setStatus(`Run ${shortId(event.run_id ?? "")} ${nextEvent.type.slice(4)}`);
      }
      if (nextEvent.type.startsWith("subagent.")) {
        void client.listTasks(event.session_id).then(setTasks).catch(() => undefined);
      }
      if (nextEvent.type === "model.usage") {
        const tokens = nextEvent.data.context_tokens;
        const limit = nextEvent.data.context_limit;
        if (typeof tokens === "number" && typeof limit === "number") {
          setContext({ tokens, limit });
        }
      }
      if (nextEvent.type === "approval.requested" || nextEvent.type === "approval.resolved") {
        void client.listApprovals(event.session_id).then(setApprovals).catch(() => undefined);
      }
      if (nextEvent.type === "approval.requested") {
        setNotice({ text: "Approval required · /approvals to list", tone: COLORS.warn });
      }
      if (nextEvent.type === "run.failed" || nextEvent.type === "run.interrupted") {
        const detail = nextEvent.data.error;
        setNotice({
          text: `Run ${nextEvent.type.slice(4)}: ${typeof detail === "string" ? detail : "no detail"}`,
          tone: COLORS.bad,
        });
      }
      // The sequence number belongs in the header, not in the message line it
      // used to overwrite on every single event.
      if (nextEvent.seq !== null) setHeadSeq(nextEvent.seq);
    });
    return unsubscribe;
  }, [client, selectedSessionId]);

  const quit = useCallback(async () => {
    setBusy(true);
    setStatus("Stopping Worker…");
    await client.close().catch((error) => setStatus(`Shutdown failed: ${errorMessage(error)}`));
    exit();
  }, [client, exit]);

  const runCommand = useCallback(async (rawCommand: string) => {
    const trimmed = rawCommand.trim();
    setCommand("");
    if (!trimmed) return;
    // The splash yields its rows to the panels as soon as there is real work.
    setSplashVisible(false);
    const parts = trimmed.split(/\s+/);
    const name = parts[0].toLowerCase().replace(/^\//, "");
    const args = parts.slice(1);
    setBusy(true);
    try {
      if (name === "quit" || name === "exit" || name === "q") {
        await quit();
        return;
      }
      if (name === "help" || name === "h") {
        setStatus("message → run · /provider NAME · /model NAME · /config · /runs · /cancel RUN_ID · /history · /fork [SEQ] · /approvals · /approve ID [once] · /skills · /skill-trust NAME · /skill-disable NAME · /skill-untrust NAME · /mcp · /tools · /quit");
        return;
      }
      if (name === "config") {
        const config = await client.getConfig(cwd);
        setStatus(
          `${config.provider.name}/${config.provider.model} · tools ${config.tools.length} · MCP ${config.mcp_servers.length} · Skill roots ${Array.isArray(config.skills.roots) ? config.skills.roots.length : 0}`,
        );
        return;
      }
      if (name === "provider") {
        const selectedName = args[0];
        if (!selectedName) throw new Error("Usage: /provider NAME [MODEL]");
        const config = await client.getConfig(cwd);
        const selected = config.providers.find((item) => item.name === selectedName.replace(/-/g, "_").toLowerCase());
        if (!selected) throw new Error(`Provider is not configured: ${selectedName}`);
        setActiveProvider(selected.name);
        setActiveModel(args[1] ?? selected.model);
        setStatus(`Selected ${selected.name}/${args[1] ?? selected.model}`);
        return;
      }
      if (name === "model") {
        const selectedModel = args[0];
        if (!selectedModel) throw new Error("Usage: /model MODEL");
        setActiveModel(selectedModel);
        setStatus(`Selected ${activeProvider}/${selectedModel}`);
        return;
      }
      if (name === "sessions" || name === "ls") {
        await refreshSessions();
        return;
      }
      if (name === "runs" || name === "run-list") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const runs = await client.listRuns(selectedSessionId);
        setStatus(
          runs.length === 0
            ? "No runs"
            : runs.slice(0, 5).map((run) => `${shortId(run.run_id)} ${run.state}${run.model ? `/${run.model}` : ""}`).join(" · "),
        );
        return;
      }
      if (name === "refresh") {
        if (selectedSessionId) await loadSession(selectedSessionId);
        await refreshSessions();
        return;
      }
      if (name === "open") {
        const target = args[0] ?? selectedSessionId;
        if (!target) throw new Error("Usage: /open SESSION_ID");
        await loadSession(target);
        return;
      }
      if (name === "new") {
        const newSession = await client.createSession({
          cwd,
          provider: args[0] ?? activeProvider,
          model: args[1] ?? activeModel,
        });
        await refreshSessions();
        await loadSession(newSession.session_id);
        return;
      }
      if (name === "run") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const userMessage = args.join(" ").trim();
        if (!userMessage) throw new Error("Usage: /run MESSAGE");
        const runId = `run_tui_${Date.now()}`;
        setActiveRunId(runId);
        setStreamText("");
        setStatus(`Running ${shortId(runId)}…`);
        const result = await client.startRun({
          session_id: selectedSessionId,
          user_message: userMessage,
          run_id: runId,
          provider: activeProvider,
          model: activeModel,
        });
        // The Worker acknowledges immediately; run.completed / run.failed and
        // the approval events drive the rest, so the UI never waits on a
        // request that lasts as long as the model takes to think.
        setStatus(`Run ${shortId(result.run_id ?? runId)} accepted`);
        return;
      }
      if (name === "resume") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const runId = args[0] ?? activeRunId;
        if (!runId) throw new Error("Usage: /resume RUN_ID");
        const result = await client.resumeRun({
          session_id: selectedSessionId,
          run_id: runId,
        });
        setActiveRunId(result.run_id ?? runId);
        setStatus(`Resume of ${shortId(runId)} accepted`);
        return;
      }
      if (name === "cancel" || name === "interrupt") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const runId = args[0];
        if (!runId) throw new Error(`Usage: /${name} RUN_ID`);
        const result = await client.cancelRun({
          session_id: selectedSessionId,
          run_id: runId,
          reason: name === "interrupt" ? "interrupted by user" : "cancelled by user",
        });
        setStatus(
          result.requested
            ? `Cancellation requested for ${shortId(result.run_id)}`
            : runStatus(result as RunResult),
        );
        if (!result.requested) setActiveRunId(undefined);
        await loadSession(selectedSessionId);
        return;
      }
      if (name === "history") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const page = await client.getSessionEvents(selectedSessionId, 0, 100);
        setStatus(`Session history · ${page.events.length} events · head ${page.head_seq}`);
        return;
      }
      if (name === "fork") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const atSeq = args[0] === undefined ? undefined : Number(args[0]);
        if (atSeq !== undefined && (!Number.isInteger(atSeq) || atSeq < 1)) {
          throw new Error("Usage: /fork [POSITIVE_SEQ]");
        }
        const child = await client.forkSession(selectedSessionId, atSeq);
        await refreshSessions();
        await loadSession(child.session_id);
        setStatus(`Forked session ${shortId(child.session_id)}`);
        return;
      }
      if (name === "approvals" || name === "approval") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        setStatus(approvalStatus(await client.listApprovals(selectedSessionId)));
        return;
      }
      if (name === "approve" || name === "deny") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const approvalId = args[0];
        if (!approvalId) throw new Error(`Usage: /${name} APPROVAL_ID${name === "approve" ? " [once]" : ""}`);
        const result = await client.resolveApproval({
          session_id: selectedSessionId,
          approval_id: approvalId,
          approved: name === "approve",
          one_shot: name === "approve" && args[1]?.toLowerCase() === "once",
          resolved_by: "tui",
        });
        setStatus(`${result.approved ? "Approved" : "Denied"} ${shortId(result.approval_id)} · use /resume RUN_ID`);
        await loadSession(selectedSessionId);
        return;
      }
      if (name === "skills") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const skills = await client.listSkills(selectedSessionId);
        setStatus(
          skills.length === 0
            ? "No Skills discovered"
            : skills.map((skill) => `${skill.name}${skill.loadable ? " ✓" : " · untrusted"}`).join(" · "),
        );
        return;
      }
      if (name === "skill-trust") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const skillName = args[0];
        if (!skillName) throw new Error("Usage: /skill-trust SKILL_NAME");
        await client.trustSkill(selectedSessionId, skillName);
        setStatus(`Trusted and enabled Skill ${skillName}`);
        return;
      }
      if (name === "skill-disable") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const skillName = args[0];
        if (!skillName) throw new Error("Usage: /skill-disable SKILL_NAME");
        await client.trustSkill(selectedSessionId, skillName, false);
        setStatus(`Disabled Skill ${skillName}`);
        return;
      }
      if (name === "skill-untrust") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const skillName = args[0];
        if (!skillName) throw new Error("Usage: /skill-untrust SKILL_NAME");
        await client.untrustSkill(selectedSessionId, skillName);
        setStatus(`Removed trust for Skill ${skillName}`);
        return;
      }
      if (name === "mcp") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const servers = await client.listMcpServers(selectedSessionId);
        setStatus(
          servers.length === 0
            ? "No MCP servers configured"
            : servers.map((server) => `${server.name} · new runs`).join(" · "),
        );
        return;
      }
      if (name === "tools") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const tools = await client.listTools(selectedSessionId);
        setStatus(tools.length === 0 ? "No tools configured" : tools.map((tool) => tool.name).join(" · "));
        return;
      }
      if (!trimmed.startsWith("/")) {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const runId = `run_tui_${Date.now()}`;
        setActiveRunId(runId);
        setStreamText("");
        setStatus(`Running ${shortId(runId)}…`);
        const result = await client.startRun({
          session_id: selectedSessionId,
          user_message: trimmed,
          run_id: runId,
          provider: activeProvider,
          model: activeModel,
        });
        // The Worker acknowledges immediately; run.completed / run.failed and
        // the approval events drive the rest, so the UI never waits on a
        // request that lasts as long as the model takes to think.
        setStatus(`Run ${shortId(result.run_id ?? runId)} accepted`);
        return;
      }
      if (name === "task") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const objective = args.join(" ").trim();
        if (!objective) throw new Error("Usage: /task OBJECTIVE");
        await client.createTask({
          session_id: selectedSessionId,
          parent_run_id: `run_tui_${Date.now()}`,
          objective,
        });
        await loadSession(selectedSessionId);
        return;
      }
      if (name === "task-start" || name === "task-done" || name === "task-cancel") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const taskId = args[0];
        if (!taskId) throw new Error(`Usage: /${name} TASK_ID`);
        const state: "running" | "completed" | "cancelled" =
          name === "task-start" ? "running" : name === "task-done" ? "completed" : "cancelled";
        await client.transitionTask({ session_id: selectedSessionId, task_id: taskId, state });
        await loadSession(selectedSessionId);
        return;
      }
      setStatus(`Unknown command: ${name} · use /help`);
    } catch (error) {
      setNotice({ text: `Error: ${errorMessage(error)}`, tone: COLORS.bad });
    } finally {
      setBusy(false);
    }
  }, [activeModel, activeProvider, activeRunId, cwd, loadSession, model, provider, quit, refreshSessions, selectedSessionId]);

  useEffect(() => {
    // Fires once, after the session exists — a prompt on the command line is
    // the first turn, not a queued command waiting for the user to press enter.
    if (!prompt || promptSent || !selectedSessionId) return;
    setPromptSent(true);
    void runCommand(prompt);
  }, [prompt, promptSent, selectedSessionId, runCommand]);

  /** Resolve the oldest pending approval, then continue its run.
   *
   * The Worker deliberately never auto-resumes: resolving is a projection
   * update, not an execution decision. Chaining the two here is the client
   * making that decision once, so the user does not have to copy an id.
   */
  const resolveOldestApproval = useCallback(
    async (approved: boolean, oneShot = false) => {
      const approval = approvals[0];
      if (!approval || !selectedSessionId) return;
      setBusy(true);
      try {
        await client.resolveApproval({
          session_id: selectedSessionId,
          approval_id: approval.approval_id,
          approved,
          one_shot: oneShot,
          resolved_by: "tui",
        });
        setApprovals(await client.listApprovals(selectedSessionId));
        if (approved && approval.run_id) {
          await client.resumeRun({
            session_id: selectedSessionId,
            run_id: approval.run_id,
          });
          setActiveRunId(approval.run_id);
          setStatus(`Approved ${shortId(approval.approval_id)} · resuming`);
        } else {
          setStatus(`${approved ? "Approved" : "Denied"} ${shortId(approval.approval_id)}`);
        }
      } catch (error) {
        setNotice({ text: `Error: ${errorMessage(error)}`, tone: COLORS.bad });
      } finally {
        setBusy(false);
      }
    },
    [approvals, client, selectedSessionId],
  );

  const awaitingApproval = approvals.length > 0;

  useInput((input, key) => {
    if (key.ctrl && input === "c") void quit();
    if (!awaitingApproval) {
      if (key.escape) setCommand("");
      return;
    }
    // While an approval is pending the prompt owns the keyboard, so a stray
    // keystroke cannot be typed into a command line that is not being shown.
    const choice = input.toLowerCase();
    if (choice === "y") void resolveOldestApproval(true);
    if (choice === "o") void resolveOldestApproval(true, true);
    if (choice === "n") void resolveOldestApproval(false);
  });

  // A run outlives the request that started it, so "busy" must follow the run.
  const running = busy || activeRunId !== undefined;

  const sessionTitle = useMemo(
    () => selectedSession?.metadata.model
      ? `${selectedSession.metadata.provider ?? activeProvider} / ${selectedSession.metadata.model ?? activeModel}`
      : `${activeProvider} / ${activeModel}`,
    [activeModel, activeProvider, selectedSession],
  );

  return (
    <Box flexDirection="column" minHeight={18} paddingX={1}>
      {splashVisible ? (
        <Splash
          cwd={cwd}
          provider={activeProvider}
          model={activeModel}
          storePath={storePath}
          configPaths={configPaths}
        />
      ) : (
        <>
          <Box justifyContent="space-between">
            <Box>
              <Text bold color={COLORS.accent}>✦ </Text>
              <GradientText bold>AI-HI!</GradientText>
            </Box>
            <Text color={COLORS.muted}>{sessionTitle}</Text>
            <Text color={running ? COLORS.warn : COLORS.good}>
              {running ? "● busy" : "● ready"}
            </Text>
          </Box>
          <Box>
            <Text color={COLORS.muted}>session  </Text>
            {selectedSessionId ? (
              <Text>{selectedSessionId}</Text>
            ) : (
              <Text color={COLORS.muted}>none · use /new</Text>
            )}
            {headSeq !== undefined && <Text color={COLORS.muted}>  seq {headSeq}</Text>}
            {activeRunId !== undefined && <Text color={COLORS.muted}>  run  {activeRunId}</Text>}
          </Box>
          {approvals.length > 0 && <ApprovalPanel approvals={approvals} />}
          <Box flexDirection="column" flexGrow={1} marginTop={1}>
            <AnswerPanel text={streamText || answerText} streaming={streamText.length > 0} />
          </Box>
        </>
      )}
      <Box marginTop={1}>
        <Text color={COLORS.muted}>{status}</Text>
      </Box>
      {notice !== undefined && (
        <Box>
          <Text bold color={notice.tone} wrap="wrap">
            {notice.text}
          </Text>
        </Box>
      )}
      {awaitingApproval ? (
        <Box>
          <Text bold color={COLORS.warn}>
            ▸ {approvals[0]?.tool_name ?? approvals[0]?.scope}
          </Text>
          <Text color={COLORS.muted}>
            {"  "}[y] allow  [o] allow once  [n] deny
          </Text>
        </Box>
      ) : (
        <Box>
          <Text color={COLORS.accent}>› </Text>
          <TextInput
            value={command}
            onChange={setCommand}
            onSubmit={(value) => void runCommand(value)}
            placeholder="Type /help for commands"
          />
        </Box>
      )}
      <StatusBar
        cwd={cwd}
        provider={activeProvider}
        model={activeModel}
        contextTokens={context.tokens}
        contextLimit={context.limit}
        tasks={tasks}
      />
      <Text color={COLORS.muted}>message  /provider  /model  /config  /runs  /cancel  /history  /fork  /approvals  /approve  /skills  /skill-trust  /skill-disable  /skill-untrust  /mcp  /tools  /resume  /quit · Ctrl-C exits</Text>
    </Box>
  );
}
