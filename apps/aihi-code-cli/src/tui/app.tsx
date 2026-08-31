import { homedir } from "node:os";
import { Box, Text, useApp, useInput, useStdout } from "ink";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AccessMode,
  ApprovalDescriptor,
  ConfigDescriptor,
  ProviderDescriptor,
  RunMode,
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
  authorityFromEventData,
  effectiveAuthority,
  formatAuthority,
  type EffectiveAuthority,
} from "./authority.js";
import {
  createComposerState,
  slashSuggestions,
  type ComposerState,
} from "./composer.js";
import { ComposerInput } from "./composer-view.js";
import { commandHelpSummary, SLASH_COMMANDS } from "./commands.js";
import { auditDiagnostic } from "./doctor.js";
import {
  filterPickerOptions,
  modelPickerOptions,
  movePickerSelection,
  pickerTitle,
  providerPickerOptions,
  sessionPickerOptions,
  type PickerMode,
  type PickerOption,
  type PickerState,
} from "./picker.js";
import { createTheme, resolveThemeName, ThemeProvider, useTheme, type Theme } from "./theme.js";
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

/** A palette tone, already resolved to a colour the terminal can render. */
type UiColor = string;

/** Notices name a tone rather than a colour, so state outlives the palette. */
type NoticeTone = "warn" | "bad";

interface CommandReport {
  title: string;
  lines: string[];
}

export interface TuiAppProps {
  client: RpcClient;
  cwd: string;
  provider: string;
  model: string;
  configuredProviders?: readonly ProviderDescriptor[];
  sessionId?: string;
  storePath?: string;
  configPaths?: string[];
  accessMode: AccessMode;
  runMode: RunMode;
  hostConsentRequired?: boolean;
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

function statusReport({
  cwd,
  selectedSession,
  selectedSessionId,
  activeProvider,
  activeModel,
  activeRunId,
  transcript,
  context,
  tasks,
  approvals,
  authority,
}: {
  cwd: string;
  selectedSession?: SessionDescriptor;
  selectedSessionId?: string;
  activeProvider: string;
  activeModel: string;
  activeRunId?: string;
  transcript: TranscriptProjection;
  context: { tokens: number; limit: number };
  tasks: TaskDescriptor[];
  approvals: ApprovalDescriptor[];
  authority: EffectiveAuthority;
}): CommandReport {
  return {
    title: "STATUS",
    lines: [
      `workspace · ${tildePath(cwd)}`,
      `session · ${selectedSessionId ?? "none"}${selectedSession ? ` · seq ${transcript.headSeq}` : ""}`,
      `model · ${activeProvider}/${activeModel}`,
      `authority · ${formatAuthority(authority)}`,
      `run · ${activeRunId ?? "ready"}${approvals.length > 0 ? ` · ${approvals.length} approval(s)` : ""}`,
      `context · ${context.limit > 0 ? `${context.tokens}/${context.limit} (${((context.tokens / context.limit) * 100).toFixed(1)}%)` : "not reported"}`,
      `tasks · ${tasks.length}`,
    ],
  };
}

function configReport(config: ConfigDescriptor): CommandReport {
  const providerSummary = config.providers
    .map((item) => `${item.name} (${item.models?.length ?? 1})`)
    .join(", ");
  return {
    title: "DOCTOR",
    lines: [
      "✓ config loaded" + (config.source_path ? ` · ${tildePath(config.source_path)}` : " · defaults"),
      `✓ providers · ${config.providers.length || 1} configured · ${providerSummary || config.provider.name} · active ${config.provider.name}/${config.provider.model}`,
      `✓ authority · ${config.access_mode} · ${config.run_mode}`,
      `✓ command sandbox · ${config.command_sandbox.backend}${config.command_sandbox.unsafe ? " · unsafe host opt-in" : ""}`,
      `✓ tools · ${config.tools.length}`,
      `audit · ${config.audit?.enabled ? config.audit.path ?? "enabled without path" : "disabled"}`,
    ],
  };
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
  const theme = useTheme();
  return (
    <Box>
      {/* A fixed, non-shrinking label column: a long value would otherwise make
          Yoga shrink this cell and swallow the padding that aligns the rows. */}
      <Box width={10} flexShrink={0}>
        <Text color={theme.muted}>{label}</Text>
      </Box>
      <Text color={theme.text} wrap="truncate-start">{value}</Text>
    </Box>
  );
}

function Splash({
  cwd,
  provider,
  model,
  configuredProviders,
  storePath,
  configPaths,
  authority,
}: {
  cwd: string;
  provider: string;
  model: string;
  configuredProviders?: readonly ProviderDescriptor[];
  storePath?: string;
  configPaths?: string[];
  authority: EffectiveAuthority;
}) {
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Banner />
      <Box flexDirection="column" marginTop={1} paddingX={2}>
        <InfoRow label="cwd" value={tildePath(cwd)} />
        <InfoRow label="provider" value={`${provider} · ${model}`} />
        <InfoRow label="authority" value={formatAuthority(authority)} />
        {configuredProviders !== undefined && configuredProviders.length > 0 && (
          <InfoRow
            label="catalog"
            value={configuredProviders
              .map((item) => `${item.name}[${(item.models ?? [item.model]).length}]`)
              .join(" · ")}
          />
        )}
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
  authority,
}: {
  cwd: string;
  provider: string;
  model: string;
  contextTokens: number;
  contextLimit: number;
  tasks: TaskDescriptor[];
  authority: EffectiveAuthority;
}) {
  const theme = useTheme();
  const ratio = contextLimit > 0 ? contextTokens / contextLimit : 0;
  const contextTone =
    ratio >= 0.9 ? theme.bad : ratio >= 0.7 ? theme.warn : theme.muted;
  const active = tasks.filter((task) => task.state === "running").length;
  return (
    <Box
      borderStyle="round"
      borderColor={theme.border}
      height={3}
      overflow="hidden"
      paddingX={1}
    >
      <Text color={theme.muted} wrap="truncate-start">
        {tildePath(cwd)}
      </Text>
      <Text color={theme.faint}>{"  ·  "}</Text>
      <Text color={theme.brand}>
        {provider}/{model}
      </Text>
      <Text color={theme.faint}>{"  ·  "}</Text>
      <Text color={theme.muted}>{formatAuthority(authority)}</Text>
      <Text color={theme.faint}>{"  ·  "}</Text>
      {contextLimit > 0 ? (
        <Text color={contextTone}>
          ctx {compactCount(contextTokens)}/{compactCount(contextLimit)}{" "}
          {(ratio * 100).toFixed(1)}%
        </Text>
      ) : (
        <Text color={theme.muted}>ctx —</Text>
      )}
      {/* The task graph earns bar space only once there is a graph to show. */}
      {tasks.length > 0 && (
        <>
          <Text color={theme.faint}>{"  ·  "}</Text>
          <Text color={active > 0 ? theme.warn : theme.good}>
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
  const theme = useTheme();
  const approval = approvals[0];
  if (approval === undefined) return null;
  const sandbox = approvalSandbox(approval);
  return (
    <Box
      borderStyle="round"
      borderColor={theme.warn}
      flexDirection="column"
      height={10}
      overflow="hidden"
      paddingX={1}
    >
      <Text bold color={theme.warn}>
        APPROVAL REQUIRED ({approvals.length})
      </Text>
      <Text color={theme.text}>
        <Text color={theme.accent}>{approval.approval_id}</Text>{" "}
        <Text bold>{approval.tool_name ?? approval.scope}</Text>
      </Text>
      <Text color={theme.muted}>[y] run · [o] once · [n] deny</Text>
      <Text color={theme.text} wrap="truncate">{approvalInput(approval).replace(/\s+/g, " ")}</Text>
      {approval.reason !== undefined && (
        <Text color={theme.muted} wrap="truncate">reason · {approval.reason}</Text>
      )}
      {approval.required_capabilities !== undefined && approval.required_capabilities.length > 0 && (
        <Text color={theme.muted}>capabilities · {approval.required_capabilities.join(", ")}</Text>
      )}
      {sandbox !== undefined && <Text color={theme.muted} wrap="truncate">sandbox · {sandbox}</Text>}
    </Box>
  );
}

function HostConsentPanel({ cwd }: { cwd: string }) {
  const theme = useTheme();
  return (
    <Box borderStyle="round" borderColor={theme.warn} flexDirection="column" paddingX={1}>
      <Text bold color={theme.warn}>TRUST THIS WORKSPACE?</Text>
      <Text color={theme.text} wrap="wrap">
        Host mode is not an isolation boundary. Commands run as your local user with this
        workspace as cwd, and may access any file or network resource allowed by the OS:
      </Text>
      <Text bold color={theme.accent}>{cwd}</Text>
      <Text color={theme.muted}>[y] trust this workspace · [n] exit</Text>
    </Box>
  );
}

/** Keyboard-first selector shared by Sessions, Providers, and Models. */
function PickerPanel({
  state,
  options,
}: {
  state: PickerState;
  options: PickerOption[];
}) {
  const theme = useTheme();
  const start = Math.min(
    Math.max(0, state.selectedIndex - 7),
    Math.max(0, options.length - 8),
  );
  const visible = options.slice(start, start + 8);
  const selected = options[state.selectedIndex];
  return (
    <Box borderStyle="round" borderColor={theme.accent} flexDirection="column" paddingX={1}>
      <Box justifyContent="space-between">
        <Text bold color={theme.accent}>{pickerTitle(state.mode)}</Text>
        <Text color={theme.faint}>{options.length} match{options.length === 1 ? "" : "es"}</Text>
      </Box>
      <Box>
        <Text color={theme.accent}>⌕ </Text>
        <Text color={theme.text}>{state.query || "Type to search…"}</Text>
      </Box>
      {visible.length === 0 ? (
        <Text color={theme.muted}>No matching entries</Text>
      ) : (
        visible.map((option, index) => {
          const absoluteIndex = start + index;
          return (
          <Box key={option.key}>
            <Text color={absoluteIndex === state.selectedIndex ? theme.accent : theme.muted}>
              {absoluteIndex === state.selectedIndex ? "› " : "  "}
            </Text>
            <Text bold={absoluteIndex === state.selectedIndex} color={absoluteIndex === state.selectedIndex ? theme.text : theme.muted}>
              {option.label}
            </Text>
            {option.detail && <Text color={theme.faint}>  {option.detail}</Text>}
          </Box>
          );
        })
      )}
      {selected !== undefined && options.length > 8 && (
        <Text color={theme.faint}>showing 8 of {options.length}</Text>
      )}
      <Text color={theme.faint}>↑/↓ select · Enter confirm · Esc close · Backspace erase</Text>
    </Box>
  );
}

function ReportPanel({ report }: { report: CommandReport }) {
  const theme = useTheme();
  return (
    <Box borderStyle="round" borderColor={theme.border} flexDirection="column" paddingX={1}>
      <Text bold color={theme.brand}>{report.title}</Text>
      {report.lines.map((line, index) => (
        <Text key={`${index}-${line}`} color={line.startsWith("✓") ? theme.good : line.startsWith("!") ? theme.warn : theme.text} wrap="truncate">
          {line}
        </Text>
      ))}
    </Box>
  );
}

function transcriptTone(theme: Theme, tone: TranscriptLineTone): UiColor | undefined {
  if (tone === "user") return theme.accent;
  if (tone === "assistant") return theme.brand;
  if (tone === "muted") return theme.muted;
  if (tone === "good") return theme.good;
  if (tone === "warn") return theme.warn;
  if (tone === "bad") return theme.bad;
  return theme.text;
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
  const theme = useTheme();
  return (
    <Box
      borderStyle="round"
      borderColor={streamText ? theme.accent : theme.border}
      flexDirection="column"
      height={rowBudget + 3}
      overflow="hidden"
      paddingX={1}
    >
      <Box justifyContent="space-between">
        <Text bold color={theme.brand}>
          CONVERSATION{streamText ? " · streaming" : ""}
        </Text>
        <Text color={viewport.followingTail ? theme.faint : theme.warn}>
          {viewport.followingTail ? "follow" : "paused"}
        </Text>
      </Box>
      {viewport.hiddenAbove > 0 && (
        <Text color={theme.faint}>↑ {viewport.hiddenAbove} earlier lines</Text>
      )}
      {viewport.lines.map((line) => (
        <Text key={line.id} bold={line.bold} color={transcriptTone(theme, line.tone)}>
          {line.text || " "}
        </Text>
      ))}
      {viewport.hiddenBelow > 0 && (
        <Text color={theme.warn}>↓ {viewport.hiddenBelow} newer lines · Ctrl-E to follow</Text>
      )}
    </Box>
  );
}


export function TuiApp({
  client,
  cwd,
  provider,
  model,
  configuredProviders,
  sessionId,
  storePath,
  configPaths,
  accessMode,
  runMode,
  hostConsentRequired = false,
  prompt,
  onSessionOpened,
}: TuiAppProps) {
  const { exit } = useApp();
  const { stdout } = useStdout();
  // Resolved once: the terminal's colour scheme does not change under us, and
  // a new palette object per render would re-paint the whole tree.
  const theme = useMemo<Theme>(() => createTheme(resolveThemeName()), []);
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
  const [authority, setAuthority] = useState<EffectiveAuthority>({
    accessMode,
    runMode,
  });
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
  const [notice, setNotice] = useState<{ text: string; tone: NoticeTone }>();
  const [report, setReport] = useState<CommandReport>();
  const [picker, setPicker] = useState<PickerState>();
  const [hostConsentPending, setHostConsentPending] = useState(hostConsentRequired);

  const openPicker = useCallback(async (mode: PickerMode, initialQuery = "") => {
    let options: PickerOption[];
    if (mode === "session") {
      const nextSessions = await client.listSessions();
      setSessions(nextSessions);
      options = sessionPickerOptions(nextSessions);
    } else {
      const config = await client.getConfig(cwd);
      const providers = config.providers.length > 0 ? config.providers : [config.provider];
      options = mode === "provider"
        ? providerPickerOptions(providers)
        : modelPickerOptions(providers);
    }
    setPicker({ mode, query: initialQuery, selectedIndex: 0, options });
    setStatus(`${pickerTitle(mode)} · type to filter`);
  }, [client, cwd]);

  const filteredPickerOptions = useMemo(
    () => picker === undefined ? [] : filterPickerOptions(picker.options, picker.query),
    [picker],
  );

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
      const [session, historyPage, nextTasks, nextApprovals, nextRuns] = await Promise.all([
        client.getSession(sessionId),
        readSessionHistory(client, sessionId),
        client.listTasks(sessionId),
        client.listApprovals(sessionId),
        client.listRuns(sessionId),
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
      setAuthority(effectiveAuthority(
        { access_mode: accessMode, run_mode: runMode },
        nextRuns,
        selectedTranscript.activeRunId,
      ));
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
  }, [accessMode, client, runMode]);

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
        tone: "bad",
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
            setNotice({ text: `${error.message} · replaying session`, tone: "warn" });
            void loadSession(event.session_id).catch((loadError) => {
              setNotice({ text: `Replay failed: ${errorMessage(loadError)}`, tone: "bad" });
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
        setAuthority((current) => authorityFromEventData(eventData, current));
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
        setNotice({ text: "Approval required · /approvals to list", tone: "warn" });
      }
      if (eventType === "run.failed" || eventType === "run.interrupted") {
        const detail = eventData.error;
        setNotice({
          text: `Run ${eventType.slice(4)}: ${typeof detail === "string" ? detail : "no detail"}`,
          tone: "bad",
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
    setReport(undefined);
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
        const providerSummary = config.providers
          .map((item) => `${item.name}[${(item.models ?? [item.model]).join(", ")}]`)
          .join(" · ");
        setStatus(
          `${providerSummary} · active ${activeProvider}/${activeModel} · ${config.access_mode}/${config.run_mode} · tools ${config.tools.length} · MCP ${config.mcp_servers.length} · Skill roots ${Array.isArray(config.skills.roots) ? config.skills.roots.length : 0}`,
        );
        return;
      }
      if (name === "status") {
        setReport(statusReport({
          cwd,
          selectedSession,
          selectedSessionId,
          activeProvider,
          activeModel,
          activeRunId,
          transcript,
          context,
          tasks,
          approvals,
          authority,
        }));
        setStatus("Status snapshot ready");
        return;
      }
      if (name === "doctor") {
        const config = await client.getConfig(cwd);
        const reportLines = configReport(config).lines;
        reportLines.push(await auditDiagnostic(config.audit));
        if (selectedSessionId) {
          const checks = await Promise.allSettled([
            client.listTools(selectedSessionId),
            client.listMcpServers(selectedSessionId),
            client.listSkills(selectedSessionId),
          ]);
          const [tools, mcp, skills] = checks;
          reportLines.push(
            tools.status === "fulfilled" ? `✓ tool registry · ${tools.value.length} available` : `! tool registry · ${errorMessage(tools.reason)}`,
            mcp.status === "fulfilled" ? `✓ MCP registry · ${mcp.value.length} configured` : `! MCP registry · ${errorMessage(mcp.reason)}`,
            skills.status === "fulfilled" ? `✓ skill registry · ${skills.value.length} discovered` : `! skill registry · ${errorMessage(skills.reason)}`,
          );
        } else {
          reportLines.push("! session-scoped checks skipped · no session");
        }
        setReport({ title: "DOCTOR", lines: reportLines });
        setStatus("Diagnostics complete");
        return;
      }
      if (name === "provider" || name === "providers") {
        const selectedName = args[0];
        if (!selectedName) {
          await openPicker("provider");
          return;
        }
        const config = await client.getConfig(cwd);
        const selected = config.providers.find((item) => item.name === selectedName.replace(/-/g, "_").toLowerCase());
        if (!selected) throw new Error(`Provider is not configured: ${selectedName}`);
        const selectedModel = args[1] ?? selected.model;
        const models = selected.models ?? [selected.model];
        if (!models.includes(selectedModel)) {
          throw new Error(`Model is not configured for ${selected.name}: ${selectedModel}`);
        }
        setActiveProvider(selected.name);
        setActiveModel(selectedModel);
        setStatus(`Selected ${selected.name}/${selectedModel}`);
        return;
      }
      if (name === "model" || name === "models") {
        const selectedModel = args[0];
        if (!selectedModel) {
          await openPicker("model");
          return;
        }
        const config = await client.getConfig(cwd);
        const separator = selectedModel.indexOf("/");
        const pairProvider = separator >= 0 ? selectedModel.slice(0, separator) : undefined;
        const pairProfile = pairProvider === undefined
          ? undefined
          : config.providers.find((item) => item.name === pairProvider);
        const requestedProvider = pairProfile === undefined ? activeProvider : pairProvider;
        const requestedModel = pairProfile === undefined
          ? selectedModel
          : selectedModel.slice(separator + 1);
        const profile = config.providers.find((item) => item.name === requestedProvider);
        const models = profile?.models ?? (profile ? [profile.model] : []);
        if (!profile) throw new Error(`Provider is not configured: ${requestedProvider}`);
        if (!models.includes(requestedModel)) {
          throw new Error(`Model is not configured for ${requestedProvider}: ${requestedModel}`);
        }
        const selectedProvider = profile.name;
        setActiveProvider(selectedProvider);
        setActiveModel(requestedModel);
        setStatus(`Selected ${selectedProvider}/${requestedModel}`);
        return;
      }
      if (name === "sessions" || name === "ls") {
        await openPicker("session", args.join(" "));
        return;
      }
      if (name === "runs" || name === "run-list") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const runs = await client.listRuns(selectedSessionId);
        setStatus(
          runs.length === 0
            ? "No runs"
            : runs.slice(0, 5).map((run) => {
                const modes = run.access_mode && run.run_mode
                  ? ` ${run.access_mode}/${run.run_mode}`
                  : "";
                return `${shortId(run.run_id)} ${run.state}${run.model ? `/${run.model}` : ""}${modes}`;
              }).join(" · "),
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
      setNotice({ text: `Error: ${errorMessage(error)}`, tone: "bad" });
    } finally {
      commandInFlightRef.current = false;
      setBusy(false);
    }
  }, [activeModel, activeProvider, activeRunId, approvals, authority, context, cwd, loadSession, model, openPicker, provider, quit, refreshSessions, resolvePendingApproval, selectedSession, selectedSessionId, tasks, transcript]);

  const selectPickerOption = useCallback(async (option: PickerOption) => {
    const mode = picker?.mode;
    setPicker(undefined);
    setBusy(true);
    try {
      if (mode === "session" && option.sessionId !== undefined) {
        await loadSession(option.sessionId);
      } else if (mode === "provider" && option.provider !== undefined) {
        setActiveProvider(option.provider);
        setActiveModel(option.model ?? activeModel);
        setStatus(`Selected ${option.provider}/${option.model ?? activeModel}`);
      } else if (mode === "model" && option.model !== undefined) {
        setActiveProvider(option.provider ?? activeProvider);
        setActiveModel(option.model);
        setStatus(`Selected ${option.provider ?? activeProvider}/${option.model}`);
      }
    } catch (error) {
      setNotice({ text: `Selection failed: ${errorMessage(error)}`, tone: "bad" });
    } finally {
      setBusy(false);
    }
  }, [activeModel, activeProvider, loadSession, picker]);

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
        setNotice({ text: `Error: ${errorMessage(error)}`, tone: "bad" });
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
      setNotice({ text: `Interrupt failed: ${errorMessage(error)}`, tone: "bad" });
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
      setNotice({ text: `Trust failed: ${errorMessage(error)}`, tone: "bad" });
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
    (report === undefined ? 0 : report.lines.length + 2) +
    (picker === undefined ? 0 : 12) +
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
    if (picker !== undefined) {
      if (key.escape) {
        setPicker(undefined);
        setStatus("Selection cancelled");
        return;
      }
      if (key.return) {
        const selected = filteredPickerOptions[picker.selectedIndex];
        if (selected !== undefined) void selectPickerOption(selected);
        return;
      }
      if (key.upArrow || key.downArrow) {
        setPicker((current) => current === undefined ? current : {
          ...current,
          selectedIndex: movePickerSelection(
            current.selectedIndex,
            filterPickerOptions(current.options, current.query).length,
            key.downArrow ? "down" : "up",
          ),
        });
        return;
      }
      if (key.backspace || key.delete) {
        setPicker((current) => current === undefined ? current : {
          ...current,
          query: Array.from(current.query).slice(0, -1).join(""),
          selectedIndex: 0,
        });
        return;
      }
      if (!key.ctrl && !key.meta && input && input !== "\n" && input !== "\r") {
        setPicker((current) => current === undefined ? current : {
          ...current,
          query: `${current.query}${input}`,
          selectedIndex: 0,
        });
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
    <ThemeProvider value={theme}>
    <Box flexDirection="column" minHeight={18} paddingX={1}>
      {splashVisible ? (
        <Splash
          cwd={cwd}
          provider={activeProvider}
          model={activeModel}
          configuredProviders={configuredProviders}
          storePath={storePath}
          configPaths={configPaths}
          authority={authority}
        />
      ) : (
        <>
          <Box justifyContent="space-between">
            <Box>
              <Text bold color={theme.accent}>✦ </Text>
              <GradientText bold>AI-HI!</GradientText>
            </Box>
            <Text color={theme.muted}>{sessionTitle}</Text>
            <Text color={running ? theme.warn : theme.good}>
              {running ? "● busy" : "● ready"}
            </Text>
          </Box>
          <Box>
            <Text color={theme.muted}>session  </Text>
            {selectedSessionId ? (
              <Text color={theme.text}>{selectedSessionId}</Text>
            ) : (
              <Text color={theme.muted}>none · use /new</Text>
            )}
            {transcript.headSeq > 0 && (
              <Text color={theme.faint}>  seq {transcript.headSeq}</Text>
            )}
            {activeRunId !== undefined && <Text color={theme.faint}>  run  {activeRunId}</Text>}
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
      {hostConsentPending && <HostConsentPanel cwd={cwd} />}
      <Box marginTop={1}>
        <Text color={theme.muted} wrap="truncate">{status}</Text>
      </Box>
      {notice !== undefined && (
        <Box>
          <Text
            bold
            color={notice.tone === "bad" ? theme.bad : theme.warn}
            wrap="truncate"
          >
            {notice.text}
          </Text>
        </Box>
      )}
      {report !== undefined && <ReportPanel report={report} />}
      {picker !== undefined && <PickerPanel state={{ ...picker, options: filteredPickerOptions }} options={filteredPickerOptions} />}
      {hostConsentPending ? null : awaitingApproval ? (
        <Box>
          <Text bold color={theme.warn}>
            ▸ {approvals[0]?.tool_name ?? approvals[0]?.scope}
          </Text>
          <Text color={theme.muted}>
            {"  "}[y] allow  [o] allow once  [n] deny
          </Text>
        </Box>
      ) : activeRunId !== undefined ? (
        <Box>
          <Text color={theme.warn}>Run in progress · Ctrl-C to interrupt</Text>
        </Box>
      ) : picker !== undefined ? null : (
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
        authority={authority}
      />
      <Text color={theme.faint} wrap="truncate">PgUp/PgDn scroll · Ctrl-E follow · Ctrl-O tool output · ↑/↓ history · Tab slash · Ctrl-W/Ctrl-U erase · Ctrl-J newline · Ctrl-C interrupt/exit</Text>
    </Box>
    </ThemeProvider>
  );
}
