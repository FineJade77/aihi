import { homedir } from "node:os";
import { Box, Text, useApp, useInput, useStdout } from "ink";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ApprovalDescriptor,
  RunResult,
  SessionDescriptor,
  TaskDescriptor,
} from "@aihi/code-protocol";
import { RpcClient } from "../rpc/client.js";
import { resolveApprovalAndResume } from "../approval.js";
import { readSessionHistory } from "../history.js";
import {
  TranscriptGapError,
  appendTranscriptEvent,
  mergeTranscriptEvents,
  projectTranscript,
  transcriptEventFromNotification,
  type TranscriptEvent,
  type TranscriptProjection,
} from "../transcript.js";
import { Banner, GradientText } from "./banner.js";
import {
  createComposerState,
  slashSuggestions,
  type ComposerState,
} from "./composer.js";
import { ComposerInput } from "./composer-view.js";
import { commandHelpSummary, SLASH_COMMANDS } from "./commands.js";
import {
  buildTranscriptLines,
  createViewportState,
  followTranscriptTail,
  scrollTranscriptViewport,
  selectTranscriptViewport,
  toggleToolDetails,
  type TranscriptLineTone,
  type TranscriptViewport,
  type TranscriptViewportState,
} from "./viewport.js";

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

export interface TuiAppProps {
  client: RpcClient;
  cwd: string;
  provider: string;
  model: string;
  sessionId?: string;
  storePath?: string;
  configPaths?: string[];
  hostConsentRequired?: boolean;
  hostExecutionRoot?: string;
  /** First turn to run once the session is up. */
  prompt?: string;
  onSessionOpened?: (sessionId: string) => void;
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

function truncate(value: string, length = 600): string {
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function approvalInput(approval: ApprovalDescriptor): string {
  const input = approval.tool_input ?? {};
  if (approval.tool_name === "bash" && typeof input.command === "string") {
    return `$ ${truncate(input.command)}`;
  }
  const path = typeof input.path === "string" ? input.path : undefined;
  if (approval.tool_name === "write_file" && path !== undefined) {
    const content = typeof input.content === "string" ? input.content : "";
    return `${path} · write ${content.length} chars\n${truncate(content)}`;
  }
  if (approval.tool_name === "edit_file" && path !== undefined) {
    const oldText = typeof input.old_text === "string" ? input.old_text : "";
    const newText = typeof input.new_text === "string" ? input.new_text : "";
    return `${path}\n- ${truncate(oldText, 240)}\n+ ${truncate(newText, 240)}`;
  }
  if (path !== undefined) return `${approval.tool_name ?? approval.scope} · ${path}`;
  const pattern = typeof input.pattern === "string" ? input.pattern : undefined;
  if (pattern !== undefined) return `${approval.tool_name ?? approval.scope} · ${pattern}`;
  return approval.tool_name ?? approval.scope;
}

function approvalSandbox(approval: ApprovalDescriptor): string | undefined {
  const sandbox = approval.sandbox;
  if (sandbox === undefined) return undefined;
  const name = typeof sandbox.name === "string" ? sandbox.name : "sandbox";
  const root = typeof sandbox.root === "string" ? sandbox.root : undefined;
  const unsafe = sandbox.unsafe === true ? "unsafe host" : name;
  return root === undefined ? unsafe : `${unsafe} · ${root}`;
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
    <Box
      borderStyle="round"
      borderColor={COLORS.panel}
      height={3}
      overflow="hidden"
      paddingX={1}
    >
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
  const approval = approvals[0];
  if (approval === undefined) return null;
  const sandbox = approvalSandbox(approval);
  return (
    <Box
      borderStyle="round"
      borderColor={COLORS.warn}
      flexDirection="column"
      height={10}
      overflow="hidden"
      paddingX={1}
    >
      <Text bold color={COLORS.warn}>
        APPROVAL REQUIRED ({approvals.length})
      </Text>
      <Text>
        <Text color={COLORS.accent}>{approval.approval_id}</Text>{" "}
        <Text bold>{approval.tool_name ?? approval.scope}</Text>
      </Text>
      <Text color={COLORS.muted}>[y] run · [o] once · [n] deny</Text>
      <Text wrap="truncate">{approvalInput(approval).replace(/\s+/g, " ")}</Text>
      {approval.reason !== undefined && (
        <Text color={COLORS.muted} wrap="truncate">reason · {approval.reason}</Text>
      )}
      {approval.required_capabilities !== undefined && approval.required_capabilities.length > 0 && (
        <Text color={COLORS.muted}>capabilities · {approval.required_capabilities.join(", ")}</Text>
      )}
      {sandbox !== undefined && <Text color={COLORS.muted} wrap="truncate">sandbox · {sandbox}</Text>}
    </Box>
  );
}

function HostConsentPanel({ cwd, root }: { cwd: string; root: string }) {
  return (
    <Box borderStyle="round" borderColor={COLORS.warn} flexDirection="column" paddingX={1}>
      <Text bold color={COLORS.warn}>TRUST THIS WORKSPACE?</Text>
      <Text wrap="wrap">
        Host mode is not an isolation boundary. AIHI tools can read and modify files and run
        commands as your local user inside this execution root:
      </Text>
      <Text color={COLORS.accent}>{root}</Text>
      {root !== cwd && <Text color={COLORS.muted}>workspace · {cwd}</Text>}
      <Text color={COLORS.muted}>[y] trust this workspace · [n] exit</Text>
    </Box>
  );
}

function transcriptTone(tone: TranscriptLineTone): UiColor | undefined {
  if (tone === "user") return COLORS.accent;
  if (tone === "assistant") return COLORS.brand;
  if (tone === "muted") return COLORS.muted;
  if (tone === "good") return COLORS.good;
  if (tone === "warn") return COLORS.warn;
  if (tone === "bad") return COLORS.bad;
  return undefined;
}

/** A line-bounded viewport over the event-derived transcript. */
function TranscriptPanel({
  viewport,
  streamText,
  rowBudget,
}: {
  viewport: TranscriptViewport;
  streamText: string;
  rowBudget: number;
}) {
  return (
    <Box
      borderStyle="round"
      borderColor={streamText ? COLORS.accent : COLORS.panel}
      flexDirection="column"
      height={rowBudget + 3}
      overflow="hidden"
      paddingX={1}
    >
      <Box justifyContent="space-between">
        <Text bold color={COLORS.brand}>
          CONVERSATION{streamText ? " · streaming" : ""}
        </Text>
        <Text color={viewport.followingTail ? COLORS.muted : COLORS.warn}>
          {viewport.followingTail ? "follow" : "paused"}
        </Text>
      </Box>
      {viewport.hiddenAbove > 0 && (
        <Text color={COLORS.muted}>↑ {viewport.hiddenAbove} earlier lines</Text>
      )}
      {viewport.lines.map((line) => (
        <Text key={line.id} bold={line.bold} color={transcriptTone(line.tone)}>
          {line.text || " "}
        </Text>
      ))}
      {viewport.hiddenBelow > 0 && (
        <Text color={COLORS.warn}>↓ {viewport.hiddenBelow} newer lines · Ctrl-E to follow</Text>
      )}
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
  hostConsentRequired = false,
  hostExecutionRoot = cwd,
  prompt,
  onSessionOpened,
}: TuiAppProps) {
  const { exit } = useApp();
  const { stdout } = useStdout();
  const [sessions, setSessions] = useState<SessionDescriptor[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | undefined>(sessionId);
  const [selectedSession, setSelectedSession] = useState<SessionDescriptor>();
  const [tasks, setTasks] = useState<TaskDescriptor[]>([]);
  const [composer, setComposer] = useState<ComposerState>(createComposerState);
  const [viewportState, setViewportState] = useState<TranscriptViewportState>(
    createViewportState,
  );
  const [terminal, setTerminal] = useState(() => ({
    columns: stdout.columns ?? 80,
    rows: stdout.rows ?? 24,
  }));
  const [status, setStatus] = useState("Connecting to Worker…");
  const [busy, setBusy] = useState(false);
  const [activeProvider, setActiveProvider] = useState(provider);
  const [activeModel, setActiveModel] = useState(model);
  const [activeRunId, setActiveRunId] = useState<string>();
  const [streamText, setStreamText] = useState("");
  const [transcript, setTranscript] = useState<TranscriptProjection>({
    headSeq: 0,
    entries: [],
  });
  const transcriptRef = useRef<TranscriptProjection>(transcript);
  const selectedSessionIdRef = useRef(selectedSessionId);
  const loadingTranscriptSessionsRef = useRef(new Map<string, number>());
  const bufferedTranscriptEventsRef = useRef(new Map<string, TranscriptEvent[]>());
  const sessionLoadGenerationRef = useRef(0);
  const commandInFlightRef = useRef(false);
  const [splashVisible, setSplashVisible] = useState(true);
  const [context, setContext] = useState({ tokens: 0, limit: 0 });
  const [promptSent, setPromptSent] = useState(false);
  const [approvals, setApprovals] = useState<ApprovalDescriptor[]>([]);
  // Kept apart from `status`: a notice must survive the event traffic that
  // follows it, which is exactly what the old status heartbeat destroyed.
  const [notice, setNotice] = useState<{ text: string; tone: UiColor }>();
  const [hostConsentPending, setHostConsentPending] = useState(hostConsentRequired);

  useEffect(() => {
    const updateTerminal = () => setTerminal({
      columns: stdout.columns ?? 80,
      rows: stdout.rows ?? 24,
    });
    updateTerminal();
    stdout.on("resize", updateTerminal);
    return () => {
      stdout.off("resize", updateTerminal);
    };
  }, [stdout]);

  const loadSession = useCallback(async (sessionId: string) => {
    const generation = sessionLoadGenerationRef.current + 1;
    sessionLoadGenerationRef.current = generation;
    // Only the newest load for a Session owns its buffering marker. An older
    // overlapping load must not keep live events trapped after the new view is
    // already published.
    loadingTranscriptSessionsRef.current.set(sessionId, generation);
    try {
      const [session, historyPage, nextTasks, nextApprovals] = await Promise.all([
        client.getSession(sessionId),
        readSessionHistory(client, sessionId),
        client.listTasks(sessionId),
        client.listApprovals(sessionId),
      ]);
      if (generation !== sessionLoadGenerationRef.current) return;
      let selectedApprovals = nextApprovals;
      let nextTranscript = projectTranscript(historyPage.events);
      let approvalsDirty = false;
      while (true) {
        const batch = bufferedTranscriptEventsRef.current.get(session.session_id) ?? [];
        bufferedTranscriptEventsRef.current.delete(session.session_id);
        if (batch.length > 0) {
          approvalsDirty ||= batch.some((event) =>
            event.type === "approval.requested" || event.type === "approval.resolved"
          );
          try {
            nextTranscript = mergeTranscriptEvents(nextTranscript, batch);
          } catch (error) {
            if (!(error instanceof TranscriptGapError)) throw error;
            // The notification stream is advisory. If it skipped a durable
            // event while replay was in flight, rebuild from the Event Store.
            const repairedHistory = await readSessionHistory(client, session.session_id);
            if (generation !== sessionLoadGenerationRef.current) return;
            nextTranscript = projectTranscript(repairedHistory.events);
            approvalsDirty = true;
          }
          continue;
        }
        if (approvalsDirty) {
          selectedApprovals = await client.listApprovals(session.session_id);
          if (generation !== sessionLoadGenerationRef.current) return;
          approvalsDirty = false;
          // Notifications received during the RPC were buffered; drain them
          // before publishing a mutually consistent transcript and snapshot.
          continue;
        }
        break;
      }
      const currentTranscript = transcriptRef.current;
      const selectedTranscript = currentTranscript.sessionId === session.session_id &&
        currentTranscript.headSeq > nextTranscript.headSeq
        ? currentTranscript
        : nextTranscript;
      transcriptRef.current = selectedTranscript;
      setTranscript(selectedTranscript);
      setActiveRunId(selectedTranscript.activeRunId);
      setApprovals(selectedApprovals);
      selectedSessionIdRef.current = session.session_id;
      setSelectedSessionId(session.session_id);
      setSelectedSession(session);
      setTasks(nextTasks);
      setStreamText("");
      setViewportState(followTranscriptTail);
      setStatus(`Session ${shortId(session.session_id)} · seq ${selectedTranscript.headSeq}`);
    } finally {
      if (loadingTranscriptSessionsRef.current.get(sessionId) === generation) {
        loadingTranscriptSessionsRef.current.delete(sessionId);
        if (generation !== sessionLoadGenerationRef.current) {
          bufferedTranscriptEventsRef.current.delete(sessionId);
        }
      }
    }
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
    selectedSessionIdRef.current = selectedSessionId;
    if (selectedSessionId) onSessionOpened?.(selectedSessionId);
  }, [selectedSessionId, onSessionOpened]);

  useEffect(() => {
    return client.subscribeRunErrors((error) => {
      if (error.session_id !== selectedSessionId) return;
      setActiveRunId(undefined);
      setNotice({
        text: `Run ${shortId(error.run_id)} failed to start: ${error.message}`,
        tone: COLORS.bad,
      });
    });
  }, [client, selectedSessionId]);

  useEffect(() => {
    const unsubscribe = client.subscribeEvents((event) => {
      if (loadingTranscriptSessionsRef.current.has(event.session_id)) {
        if (!event.ephemeral) {
          const buffered = bufferedTranscriptEventsRef.current.get(event.session_id) ?? [];
          buffered.push(transcriptEventFromNotification(event));
          bufferedTranscriptEventsRef.current.set(event.session_id, buffered);
        }
        return;
      }
      if (event.session_id !== selectedSessionIdRef.current) return;
      if (!event.ephemeral) {
        try {
          const nextTranscript = appendTranscriptEvent(
            transcriptRef.current,
            transcriptEventFromNotification(event),
          );
          transcriptRef.current = nextTranscript;
          setTranscript(nextTranscript);
        } catch (error) {
          if (error instanceof TranscriptGapError) {
            setNotice({ text: `${error.message} · replaying session`, tone: COLORS.warn });
            void loadSession(event.session_id).catch((loadError) => {
              setNotice({ text: `Replay failed: ${errorMessage(loadError)}`, tone: COLORS.bad });
            });
          } else {
            throw error;
          }
        }
      }
      const eventType = event.event_type;
      const eventData = event.data;
      if (eventType === "run.started" || eventType === "run.resumed") {
        setActiveRunId(event.run_id);
        setStreamText("");
        setNotice(undefined);
      }
      if (eventType === "model.chunk" && eventData.kind === "text_delta") {
        const delta = eventData.text;
        if (typeof delta === "string") setStreamText((current) => `${current}${delta}`);
      }
      if (eventType === "assistant.message") {
        // The persisted message is authoritative; drop the accumulated deltas.
        setStreamText("");
      }
      if (["run.completed", "run.failed", "run.interrupted", "run.cancelled"].includes(eventType)) {
        setActiveRunId(undefined);
        setStatus(`Run ${shortId(event.run_id ?? "")} ${eventType.slice(4)}`);
      }
      if (eventType.startsWith("subagent.")) {
        void client.listTasks(event.session_id).then(setTasks).catch(() => undefined);
      }
      if (eventType === "model.usage") {
        const tokens = eventData.context_tokens;
        const limit = eventData.context_limit;
        if (typeof tokens === "number" && typeof limit === "number") {
          setContext({ tokens, limit });
        }
      }
      if (eventType === "approval.requested" || eventType === "approval.resolved") {
        void client.listApprovals(event.session_id).then(setApprovals).catch(() => undefined);
      }
      if (eventType === "approval.requested") {
        setNotice({ text: "Approval required · /approvals to list", tone: COLORS.warn });
      }
      if (eventType === "run.failed" || eventType === "run.interrupted") {
        const detail = eventData.error;
        setNotice({
          text: `Run ${eventType.slice(4)}: ${typeof detail === "string" ? detail : "no detail"}`,
          tone: COLORS.bad,
        });
      }
      // The sequence number belongs in the header, not in the message line it
      // used to overwrite on every single event.
    });
    return unsubscribe;
  }, [client, loadSession]);

  const quit = useCallback(async () => {
    setBusy(true);
    setStatus("Stopping Worker…");
    await client.close().catch((error) => setStatus(`Shutdown failed: ${errorMessage(error)}`));
    exit();
  }, [client, exit]);

  const resolvePendingApproval = useCallback(async (
    approvalId: string,
    approved: boolean,
    oneShot = false,
  ) => {
    if (!selectedSessionId) throw new Error("Create or open a session first");
    try {
      const { run } = await resolveApprovalAndResume(client, {
        session_id: selectedSessionId,
        approval_id: approvalId,
        approved,
        one_shot: oneShot,
        resolved_by: "tui",
      });
      setApprovals(await client.listApprovals(selectedSessionId));
      setActiveRunId(run.run_id);
      setStatus(`${approved ? "Approved" : "Denied"} ${shortId(approvalId)} · resuming`);
    } catch (error) {
      // Resolution may have persisted before resume failed. Unlock the composer
      // so the operator can inspect /runs and retry /resume explicitly.
      setActiveRunId(undefined);
      throw error;
    }
  }, [client, selectedSessionId]);

  const runCommand = useCallback(async (rawCommand: string) => {
    const trimmed = rawCommand.trim();
    if (!trimmed || commandInFlightRef.current) return;
    commandInFlightRef.current = true;
    // The splash yields its rows to the panels as soon as there is real work.
    setSplashVisible(false);
    const parts = trimmed.split(/\s+/);
    const name = parts[0].toLowerCase().replace(/^\//, "");
    const args = parts.slice(1);
    let submittedRunId: string | undefined;
    setBusy(true);
    try {
      if (name === "quit" || name === "exit" || name === "q") {
        await quit();
        return;
      }
      if (name === "help" || name === "h") {
        setStatus(`message → run · ${commandHelpSummary()}`);
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
        submittedRunId = runId;
        setActiveRunId(runId);
        setStreamText("");
        setViewportState(followTranscriptTail);
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
        setStatus(`Run ${shortId(result.run_id)} accepted`);
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
        setActiveRunId(result.run_id);
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
        const history = await readSessionHistory(client, selectedSessionId);
        setStatus(`Session history · ${history.events.length} events · head ${history.headSeq}`);
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
        await resolvePendingApproval(
          approvalId,
          name === "approve",
          name === "approve" && args[1]?.toLowerCase() === "once",
        );
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
        submittedRunId = runId;
        setActiveRunId(runId);
        setStreamText("");
        setViewportState(followTranscriptTail);
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
        setStatus(`Run ${shortId(result.run_id)} accepted`);
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
      if (submittedRunId !== undefined) {
        setActiveRunId((current) => current === submittedRunId ? undefined : current);
      }
      setNotice({ text: `Error: ${errorMessage(error)}`, tone: COLORS.bad });
    } finally {
      commandInFlightRef.current = false;
      setBusy(false);
    }
  }, [activeModel, activeProvider, activeRunId, cwd, loadSession, model, provider, quit, refreshSessions, resolvePendingApproval, selectedSessionId]);

  useEffect(() => {
    // Fires once, after the session exists — a prompt on the command line is
    // the first turn, not a queued command waiting for the user to press enter.
    if (!prompt || promptSent || !selectedSessionId || hostConsentPending) return;
    setPromptSent(true);
    void runCommand(prompt);
  }, [hostConsentPending, prompt, promptSent, selectedSessionId, runCommand]);

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
        await resolvePendingApproval(approval.approval_id, approved, oneShot);
      } catch (error) {
        setNotice({ text: `Error: ${errorMessage(error)}`, tone: COLORS.bad });
      } finally {
        setBusy(false);
      }
    },
    [approvals, resolvePendingApproval, selectedSessionId],
  );

  const awaitingApproval = approvals.length > 0;

  const interruptRun = useCallback(async () => {
    if (!selectedSessionId || !activeRunId) return;
    setStatus(`Interrupting ${shortId(activeRunId)}…`);
    try {
      const result = await client.cancelRun({
        session_id: selectedSessionId,
        run_id: activeRunId,
        reason: "interrupted by user",
      });
      if (result.requested) {
        setStatus(`Interruption requested for ${shortId(activeRunId)}`);
      } else {
        setActiveRunId(undefined);
        setStatus(runStatus(result as RunResult));
      }
    } catch (error) {
      setNotice({ text: `Interrupt failed: ${errorMessage(error)}`, tone: COLORS.bad });
    }
  }, [activeRunId, client, selectedSessionId]);

  const resolveHostConsent = useCallback(async (approved: boolean) => {
    if (!approved) {
      await quit();
      return;
    }
    setBusy(true);
    setStatus("Saving workspace trust…");
    try {
      await client.acknowledgeHost(cwd);
      setHostConsentPending(false);
      setStatus("Workspace trusted for Host execution");
    } catch (error) {
      setNotice({ text: `Trust failed: ${errorMessage(error)}`, tone: COLORS.bad });
    } finally {
      setBusy(false);
    }
  }, [client, cwd, quit]);

  const transcriptColumns = Math.max(20, terminal.columns - 6);
  const composerLines = composer.value.split("\n").length;
  const suggestionRows = slashSuggestions(composer, SLASH_COMMANDS).length;
  const transcriptRowBudget = Math.max(4, terminal.rows - (
    13 +
    (notice === undefined ? 0 : 1) +
    (approvals.length > 0 ? 10 : 0) +
    (composerLines > 1 ? Math.min(3, composerLines) : 0) +
    (suggestionRows > 0 ? suggestionRows + 1 : 0)
  ));
  const transcriptLines = useMemo(
    () => buildTranscriptLines(
      transcript.entries,
      streamText,
      transcriptColumns,
      viewportState.expandedToolDetails,
    ),
    [streamText, transcript.entries, transcriptColumns, viewportState.expandedToolDetails],
  );
  const transcriptViewport = useMemo(
    () => selectTranscriptViewport(transcriptLines, viewportState, transcriptRowBudget),
    [transcriptLines, transcriptRowBudget, viewportState],
  );
  useInput((input, key) => {
    if (hostConsentPending) {
      if (key.ctrl && input === "c") void quit();
      if (busy) return;
      if (!key.ctrl && !key.meta && input.toLowerCase() === "y") {
        void resolveHostConsent(true);
      }
      if (!key.ctrl && !key.meta && input.toLowerCase() === "n") {
        void resolveHostConsent(false);
      }
      return;
    }
    if (key.ctrl && input === "c") {
      if (activeRunId) void interruptRun();
      else void quit();
      return;
    }
    if (key.pageUp || (key.ctrl && key.upArrow)) {
      setSplashVisible(false);
      setViewportState((current) => scrollTranscriptViewport(
        current,
        transcriptLines,
        transcriptRowBudget,
        "up",
      ));
      return;
    }
    if (key.pageDown || (key.ctrl && key.downArrow)) {
      setViewportState((current) => scrollTranscriptViewport(
        current,
        transcriptLines,
        transcriptRowBudget,
        "down",
      ));
      return;
    }
    if (key.ctrl && input === "e") {
      setViewportState(followTranscriptTail);
      return;
    }
    if (key.ctrl && input === "o") {
      setSplashVisible(false);
      setViewportState(toggleToolDetails);
      return;
    }
    if (awaitingApproval) {
      if (busy || key.ctrl || key.meta) return;
      // While an approval is pending the prompt owns unmodified y/o/n, so a
      // stray keystroke cannot enter a hidden composer or grant by accident.
      const choice = input.toLowerCase();
      if (choice === "y") void resolveOldestApproval(true);
      if (choice === "o") void resolveOldestApproval(true, true);
      if (choice === "n") void resolveOldestApproval(false);
      return;
    }
    // The composer owns all remaining editing keys in its own useInput hook.
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
            {transcript.headSeq > 0 && (
              <Text color={COLORS.muted}>  seq {transcript.headSeq}</Text>
            )}
            {activeRunId !== undefined && <Text color={COLORS.muted}>  run  {activeRunId}</Text>}
          </Box>
          {approvals.length > 0 && <ApprovalPanel approvals={approvals} />}
          <Box flexDirection="column" flexGrow={1} marginTop={1}>
            <TranscriptPanel
              viewport={transcriptViewport}
              streamText={streamText}
              rowBudget={transcriptRowBudget}
            />
          </Box>
        </>
      )}
      {hostConsentPending && <HostConsentPanel cwd={cwd} root={hostExecutionRoot} />}
      <Box marginTop={1}>
        <Text color={COLORS.muted} wrap="truncate">{status}</Text>
      </Box>
      {notice !== undefined && (
        <Box>
          <Text bold color={notice.tone} wrap="truncate">
            {notice.text}
          </Text>
        </Box>
      )}
      {hostConsentPending ? null : awaitingApproval ? (
        <Box>
          <Text bold color={COLORS.warn}>
            ▸ {approvals[0]?.tool_name ?? approvals[0]?.scope}
          </Text>
          <Text color={COLORS.muted}>
            {"  "}[y] allow  [o] allow once  [n] deny
          </Text>
        </Box>
      ) : activeRunId !== undefined ? (
        <Box>
          <Text color={COLORS.warn}>Run in progress · Ctrl-C to interrupt</Text>
        </Box>
      ) : (
        <ComposerInput
          state={composer}
          commands={SLASH_COMMANDS}
          active={!busy}
          onChange={setComposer}
          onSubmit={(value) => void runCommand(value)}
        />
      )}
      <StatusBar
        cwd={cwd}
        provider={activeProvider}
        model={activeModel}
        contextTokens={context.tokens}
        contextLimit={context.limit}
        tasks={tasks}
      />
      <Text color={COLORS.muted} wrap="truncate">PgUp/PgDn scroll · Ctrl-E follow · Ctrl-O tool output · ↑/↓ history · Tab slash · Ctrl-J newline · Ctrl-C interrupt/exit</Text>
    </Box>
  );
}
