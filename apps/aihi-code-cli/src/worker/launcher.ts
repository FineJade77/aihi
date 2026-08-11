import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";

export interface WorkerLaunchOptions {
  /** Python executable; defaults to AIHI_CODE_AGENT_PYTHON or python3. */
  command?: string;
  /** Python arguments; defaults to the bundled worker module. */
  args?: string[];
  cwd?: string;
  env?: Record<string, string | undefined>;
}
export function resolveWorkerCommand(options: WorkerLaunchOptions = {}): {
  command: string;
  args: string[];
} {
  return {
    command: options.command ?? process.env.AIHI_CODE_AGENT_PYTHON ?? "python3",
    args: options.args ?? ["-m", "aihi.code_agent.worker"],
  };
}

/** Launches a dedicated Python process with binary stdio for framed RPC. */
export function launchWorker(options: WorkerLaunchOptions = {}): ChildProcessWithoutNullStreams {
  const { command, args } = resolveWorkerCommand(options);
  return spawn(command, args, {
    cwd: options.cwd,
    env: {
      ...process.env,
      ...options.env,
      // Keep stdout reserved for protocol frames. Diagnostics belong on stderr.
      PYTHONUNBUFFERED: "1",
    },
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
  });
}
