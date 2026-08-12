import { mkdir, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import * as process from "node:process";
import { bootstrapSession } from "./bootstrap.js";
import { RpcClient } from "./rpc/client.js";
import { runTui } from "./tui/index.js";

export interface CliOptions {
  cwd: string;
  storePath?: string;
  sessionId?: string;
  continueSession: boolean;
  provider?: string;
  model?: string;
  /** Free-form words after the flags: the first turn to run on startup. */
  prompt?: string;
}

export function parseArgs(argv: string[]): CliOptions {
  const options: CliOptions = {
    cwd: process.cwd(),
    storePath: process.env.AIHI_CODE_AGENT_STORE,
    continueSession: false,
    provider: process.env.AIHI_CODE_AGENT_PROVIDER,
    model: process.env.AIHI_CODE_AGENT_MODEL,
  };
  const prompt: string[] = [];
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--continue" || argument === "-c") {
      options.continueSession = true;
      continue;
    }
    if (argument === "--cwd" || argument === "--workspace" || argument === "--store" || argument === "--provider" || argument === "--model" || argument === "--session") {
      const value = argv[index + 1];
      if (!value) throw new Error(`${argument} requires a value`);
      if (argument === "--cwd" || argument === "--workspace") options.cwd = value;
      if (argument === "--store") options.storePath = value;
      if (argument === "--provider") options.provider = value;
      if (argument === "--model") options.model = value;
      if (argument === "--session") options.sessionId = value;
      index += 1;
      continue;
    }
    if (argument === "--help" || argument === "-h") {
      process.stdout.write(
        "Usage: aihi-code [OPTIONS] [PROMPT...]\n\n" +
          "Starts an interactive coding session. With PROMPT, runs it as the first turn.\n\n" +
          "  --workspace PATH   Workspace to operate in (default: cwd)\n" +
          "  --cwd PATH         Alias for --workspace\n" +
          "  --store PATH       SQLite event store (default: ~/.aihi/sessions.sqlite3)\n" +
          "  --provider NAME    Provider profile to use\n" +
          "  --model NAME       Model to use\n" +
          "  --session ID       Open a known session in this workspace\n" +
          "  --continue, -c     Open the latest session in this workspace\n",
      );
      process.exit(0);
    }
    if (argument.startsWith("-")) {
      throw new Error(`Unknown argument: ${argument}`);
    }
    // Everything from the first bare word on is the prompt, so a quoted or
    // unquoted sentence both work.
    prompt.push(...argv.slice(index));
    break;
  }
  if (options.sessionId !== undefined && options.continueSession) {
    throw new Error("--session and --continue cannot be used together");
  }
  if (prompt.length > 0) options.prompt = prompt.join(" ");
  return options;
}

export function defaultStorePath(home = homedir()): string {
  return join(home, ".aihi", "sessions.sqlite3");
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  const options = parseArgs(argv);
  const cwd = resolve(options.cwd);
  const workspace = await stat(cwd).catch(() => undefined);
  if (workspace === undefined || !workspace.isDirectory()) {
    throw new Error(`Workspace is not a directory: ${cwd}`);
  }
  const storePath = resolve(options.storePath ?? defaultStorePath());
  await mkdir(dirname(storePath), { recursive: true, mode: 0o700 });
  const client = await RpcClient.connect({
    cwd,
    storePath,
  });
  try {
    const config = await client.initConfig();
    if (config.created) {
      process.stderr.write(`aihi-code: wrote default configuration to ${config.path}\n`);
    }
    const bootstrap = await bootstrapSession(client, {
      cwd,
      sessionId: options.sessionId,
      continueSession: options.continueSession,
      provider: options.provider,
      model: options.model,
    });
    let lastSessionId: string | undefined;
    await runTui({
      client,
      prompt: options.prompt,
      onSessionOpened: (id) => {
        lastSessionId = id;
      },
      cwd: bootstrap.cwd,
      provider: bootstrap.provider,
      model: bootstrap.model,
      sessionId: bootstrap.session.session_id,
      storePath,
      configPaths: bootstrap.config.source_paths,
    });
    if (lastSessionId !== undefined) {
      process.stderr.write(
        `\naihi-code: session ${lastSessionId} closed.\n` +
          `  Resume it with: aihi-code --workspace ${bootstrap.cwd} --session ${lastSessionId}\n`,
      );
    }
  } finally {
    await client.close();
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error: unknown) => {
    process.stderr.write(`aihi-code: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(1);
  });
}
