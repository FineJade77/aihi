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

function stringifyPreview(value: unknown, maxLength = 84): string {
  let text: string;
  try {
    text = JSON.stringify(value) ?? "{}";
  } catch {
    text = "[unserializable]";
  }
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function taskColor(state: string): UiColor {
  if (state === "completed") return COLORS.good;
  if (state === "failed" || state === "cancelled") return COLORS.bad;
  if (state === "waiting" || state === "interrupted") return COLORS.warn;
  return COLORS.brand;
}

function shortId(value: string): string {
  return value.length > 16 ? `…${value.slice(-14)}` : value;
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

function SessionPanel({
  sessions,
  selectedSessionId,
}: {
  sessions: SessionDescriptor[];
  selectedSessionId?: string;
}) {
  return (
    <Box borderStyle="round" borderColor={COLORS.panel} flexDirection="column" paddingX={1} width="28%">
      <Text bold color={COLORS.brand}>SESSIONS</Text>
      {sessions.length === 0 ? (
        <Text color={COLORS.muted}>No sessions</Text>
      ) : (
        sessions.slice(0, 8).map((session) => {
          const selected = session.session_id === selectedSessionId;
          return (
            <Text key={session.session_id} color={selected ? COLORS.accent : undefined}>
              {selected ? "▸ " : "  "}{shortId(session.session_id)}
              {" "}{String(session.metadata.model ?? "")}
            </Text>
          );
        })
      )}
      {sessions.length > 8 && <Text color={COLORS.muted}>+{sessions.length - 8} more</Text>}
    </Box>
  );
}

function EventPanel({ events }: { events: UiEvent[] }) {
  return (
    <Box borderStyle="round" borderColor={COLORS.panel} flexDirection="column" paddingX={1} width="44%">
      <Text bold color={COLORS.brand}>EVENT STREAM</Text>
      {events.length === 0 ? (
        <Text color={COLORS.muted}>Waiting for events…</Text>
      ) : (
        events.slice(-12).map((event, index) => (
          <Text key={`${event.seq ?? "e"}-${event.type}-${index}`} wrap="truncate">
            <Text color={event.ephemeral ? COLORS.muted : COLORS.good}>
              {event.ephemeral ? "·" : "●"} {event.seq ?? "-"}
            </Text>{" "}
            <Text color={COLORS.accent}>{event.type}</Text>{" "}
            <Text color={COLORS.muted}>{stringifyPreview(event.data)}</Text>
          </Text>
        ))
      )}
    </Box>
  );
}

function TaskPanel({ tasks }: { tasks: TaskDescriptor[] }) {
  return (
    <Box borderStyle="round" borderColor={COLORS.panel} flexDirection="column" paddingX={1} width="28%">
      <Text bold color={COLORS.brand}>TASK GRAPH</Text>
      {tasks.length === 0 ? (
        <Text color={COLORS.muted}>No tasks</Text>
      ) : (
        tasks.slice(0, 8).map((task) => (
          <Text key={String(task.spec.task_id)} wrap="truncate">
            <Text color={taskColor(task.state)}>{task.state.padEnd(11, " ")}</Text>{" "}
            {shortId(String(task.spec.task_id))}
          </Text>
        ))
      )}
    </Box>
  );
}

export function TuiApp({ client, cwd, provider, model, sessionId }: TuiAppProps) {
  const { exit } = useApp();
  const [sessions, setSessions] = useState<SessionDescriptor[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | undefined>(sessionId);
  const [selectedSession, setSelectedSession] = useState<SessionDescriptor>();
  const [events, setEvents] = useState<UiEvent[]>([]);
  const [tasks, setTasks] = useState<TaskDescriptor[]>([]);
  const [command, setCommand] = useState("");
  const [status, setStatus] = useState("Connecting to Worker…");
  const [busy, setBusy] = useState(false);
  const [activeProvider, setActiveProvider] = useState(provider);
  const [activeModel, setActiveModel] = useState(model);

  const loadSession = useCallback(async (sessionId: string) => {
    const [session, page, nextTasks] = await Promise.all([
      client.getSession(sessionId),
      client.getSessionEvents(sessionId, 0, 100),
      client.listTasks(sessionId),
    ]);
    setSelectedSessionId(session.session_id);
    setSelectedSession(session);
    setEvents(page.events.map(eventFromRecord));
    setTasks(nextTasks);
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
      setEvents([]);
      setTasks([]);
      setStatus("No session yet · use /new to create one");
    }
  }, [client, loadSession, selectedSessionId]);

  useEffect(() => {
    void client.getConfig(cwd).then((config) => {
      setActiveProvider(config.provider.name);
      setActiveModel(config.provider.model);
    }).catch((error) => setStatus(`Config load failed: ${errorMessage(error)}`));
  }, [client, cwd]);

  useEffect(() => {
    void refreshSessions().catch((error) => setStatus(`Load failed: ${errorMessage(error)}`));
  // Initial Worker/session discovery is intentionally run once per Worker.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  useEffect(() => {
    const unsubscribe = client.subscribeEvents((event) => {
      if (event.session_id !== selectedSessionId) return;
      const nextEvent = eventFromNotification(event);
      setEvents((current) => [...current, nextEvent].slice(-100));
      if (nextEvent.type.startsWith("subagent.")) {
        void client.listTasks(event.session_id).then(setTasks).catch(() => undefined);
      }
      if (nextEvent.seq !== null) {
        setStatus(`Session ${shortId(event.session_id)} · seq ${nextEvent.seq}`);
      }
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
        setStatus("message → run · /provider NAME · /model NAME · /config · /runs · /cancel RUN_ID · /history · /fork [SEQ] · /approvals · /approve ID [once] · /skills · /skill-trust NAME · /quit");
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
        const result = await client.startRun({
          session_id: selectedSessionId,
          user_message: userMessage,
          provider: activeProvider,
          model: activeModel,
        });
        setStatus(runStatus(result));
        await loadSession(selectedSessionId);
        return;
      }
      if (name === "resume") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const runId = args[0];
        if (!runId) throw new Error("Usage: /resume RUN_ID");
        const result = await client.resumeRun({
          session_id: selectedSessionId,
          run_id: runId,
        });
        setStatus(runStatus(result));
        await loadSession(selectedSessionId);
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
        setStatus(runStatus(result));
        await loadSession(selectedSessionId);
        return;
      }
      if (name === "history") {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const page = await client.getSessionEvents(selectedSessionId, 0, 100);
        setStatus(`Session history · ${page.events.length} events · head ${page.head_seq}`);
        setEvents(page.events.map(eventFromRecord));
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
      if (!trimmed.startsWith("/")) {
        if (!selectedSessionId) throw new Error("Create or open a session first");
        const result = await client.startRun({
          session_id: selectedSessionId,
          user_message: trimmed,
          provider: activeProvider,
          model: activeModel,
        });
        setStatus(runStatus(result));
        await loadSession(selectedSessionId);
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
      setStatus(`Error: ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  }, [activeModel, activeProvider, cwd, loadSession, model, provider, quit, refreshSessions, selectedSessionId]);

  useInput((input, key) => {
    if (key.ctrl && input === "c") void quit();
    if (key.escape) setCommand("");
  });

  const sessionTitle = useMemo(
    () => selectedSession?.metadata.model
      ? `${selectedSession.metadata.provider ?? activeProvider} / ${selectedSession.metadata.model ?? activeModel}`
      : `${activeProvider} / ${activeModel}`,
    [activeModel, activeProvider, selectedSession],
  );

  return (
    <Box flexDirection="column" minHeight={18} paddingX={1}>
      <Box justifyContent="space-between">
        <Text bold color={COLORS.brand}>✦ AIHI CODE AGENT</Text>
        <Text color={COLORS.muted}>{sessionTitle}</Text>
        <Text color={busy ? COLORS.warn : COLORS.good}>{busy ? "● busy" : "● ready"}</Text>
      </Box>
      <Text color={COLORS.muted} wrap="truncate">{cwd}</Text>
      <Box flexDirection="row" flexGrow={1} marginTop={1}>
        <SessionPanel sessions={sessions} selectedSessionId={selectedSessionId} />
        <EventPanel events={events} />
        <TaskPanel tasks={tasks} />
      </Box>
      <Box marginTop={1}>
        <Text color={COLORS.muted}>{status}</Text>
      </Box>
      <Box>
        <Text color={COLORS.accent}>› </Text>
        <TextInput
          value={command}
          onChange={setCommand}
          onSubmit={(value) => void runCommand(value)}
          placeholder="Type /help for commands"
        />
      </Box>
      <Text color={COLORS.muted}>message  /provider  /model  /config  /runs  /cancel  /history  /fork  /approvals  /approve  /skills  /skill-trust  /resume  /quit · Ctrl-C exits</Text>
    </Box>
  );
}
