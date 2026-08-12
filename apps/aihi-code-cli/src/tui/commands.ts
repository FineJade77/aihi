import type { SlashCommandDescriptor } from "./composer.js";

export const SLASH_COMMANDS: readonly SlashCommandDescriptor[] = [
  { name: "help", usage: "/help", description: "Show command help" },
  { name: "new", usage: "/new [PROVIDER] [MODEL]", description: "Create a Session" },
  { name: "open", usage: "/open SESSION_ID", description: "Open a Session" },
  { name: "sessions", usage: "/sessions", description: "List and refresh Sessions" },
  { name: "run", usage: "/run MESSAGE", description: "Start a Run" },
  { name: "runs", usage: "/runs", description: "List Runs" },
  { name: "resume", usage: "/resume RUN_ID", description: "Resume a suspended Run" },
  { name: "cancel", usage: "/cancel RUN_ID", description: "Cancel a Run" },
  { name: "interrupt", usage: "/interrupt RUN_ID", description: "Interrupt a Run" },
  { name: "provider", usage: "/provider NAME [MODEL]", description: "Select a Provider" },
  { name: "model", usage: "/model MODEL", description: "Select a model" },
  { name: "config", usage: "/config", description: "Show resolved config" },
  { name: "refresh", usage: "/refresh", description: "Replay the current Session" },
  { name: "history", usage: "/history", description: "Inspect Event history" },
  { name: "fork", usage: "/fork [SEQ]", description: "Fork the current Session" },
  { name: "approvals", usage: "/approvals", description: "List pending approvals" },
  { name: "approve", usage: "/approve ID [once]", description: "Grant an approval" },
  { name: "deny", usage: "/deny ID", description: "Deny an approval" },
  { name: "skills", usage: "/skills", description: "List discovered Skills" },
  { name: "skill-trust", usage: "/skill-trust NAME", description: "Trust and enable a Skill" },
  { name: "skill-disable", usage: "/skill-disable NAME", description: "Disable a Skill" },
  { name: "skill-untrust", usage: "/skill-untrust NAME", description: "Remove Skill trust" },
  { name: "mcp", usage: "/mcp", description: "List MCP servers" },
  { name: "tools", usage: "/tools", description: "List Tools" },
  { name: "task", usage: "/task OBJECTIVE", description: "Create a Task" },
  { name: "task-start", usage: "/task-start TASK_ID", description: "Start a Task" },
  { name: "task-done", usage: "/task-done TASK_ID", description: "Complete a Task" },
  { name: "task-cancel", usage: "/task-cancel TASK_ID", description: "Cancel a Task" },
  { name: "quit", usage: "/quit", description: "Exit AIHI Code" },
] as const;

export function commandHelpSummary(): string {
  return SLASH_COMMANDS.map((command) => command.usage).join(" · ");
}
