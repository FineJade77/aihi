import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  AgentEvent,
  EventRecord,
  JsonObject,
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

export function TuiApp({ client, cwd, provider, model }: TuiAppProps) {
  const { exit } = useApp();
  const [sessions, setSessions] = useState<SessionDescriptor[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string>();
  const [selectedSession, setSelectedSession] = useState<SessionDescriptor>();
  const [events, setEvents] = useState<UiEvent[]>([]);
  const [tasks, setTasks] = useState<TaskDescriptor[]>([]);
  const [command, setCommand] = useState("");
  const [status, setStatus] = useState("Connecting to Worker…");
  const [busy, setBusy] = useState(false);

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
        setStatus("/new [provider model] · /open ID · /sessions · /refresh · /task TEXT · /quit");
        return;
      }
      if (name === "sessions" || name === "ls") {
        await refreshSessions();
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
          provider: args[0] ?? provider,
          model: args[1] ?? model,
        });
        await refreshSessions();
        await loadSession(newSession.session_id);
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
  }, [cwd, loadSession, model, provider, quit, refreshSessions, selectedSessionId]);

  useInput((input, key) => {
    if (key.ctrl && input === "c") void quit();
    if (key.escape) setCommand("");
  });

  const sessionTitle = useMemo(
    () => selectedSession?.metadata.model
      ? `${selectedSession.metadata.provider ?? provider} / ${selectedSession.metadata.model}`
      : "No active session",
    [provider, selectedSession],
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
      <Text color={COLORS.muted}>/new  /open  /sessions  /task  /refresh  /quit · Ctrl-C exits</Text>
    </Box>
  );
}
