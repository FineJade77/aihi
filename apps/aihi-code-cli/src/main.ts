import * as process from "node:process";
import { RpcClient } from "./rpc/client.js";
import { runTui } from "./tui/index.js";

interface CliOptions {
  cwd: string;
  storePath?: string;
  sessionId?: string;
  provider: string;
  model: string;
  /** Free-form words after the flags: the first turn to run on startup. */
  prompt?: string;
}

function parseArgs(argv: string[]): CliOptions {
  const options: CliOptions = {
    cwd: process.cwd(),
    storePath: process.env.AIHI_CODE_AGENT_STORE,
    provider: process.env.AIHI_CODE_AGENT_PROVIDER ?? "fake",
    model: process.env.AIHI_CODE_AGENT_MODEL ?? "demo",
  };
  const prompt: string[] = [];
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
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
          "Starts a new session. With PROMPT, runs it as the first turn.\n\n" +
          "  --workspace PATH   Workspace to operate in (default: cwd)\n" +
          "  --cwd PATH         Alias for --workspace\n" +
          "  --store PATH       SQLite event store (default: in-memory)\n" +
          "  --provider NAME    Provider profile to use\n" +
          "  --model NAME       Model to use\n" +
          "  --session ID       Resume an existing session instead of starting one\n",
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
  if (prompt.length > 0) options.prompt = prompt.join(" ");
  return options;
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  const options = parseArgs(argv);
  const client = await RpcClient.connect({
    cwd: options.cwd,
    storePath: options.storePath,
  });
  try {
    const config = await client.initConfig();
    if (config.created) {
      process.stderr.write(`aihi-code: wrote default configuration to ${config.path}\n`);
    }
    let lastSessionId: string | undefined;
    await runTui({
      client,
      prompt: options.prompt,
      onSessionOpened: (id) => {
        lastSessionId = id;
      },
      cwd: options.cwd,
      provider: options.provider,
      model: options.model,
      sessionId: options.sessionId,
      storePath: options.storePath,
      configPath: config.path,
    });
    if (lastSessionId !== undefined) {
      const store = options.storePath ? "" : "  (needs --store to persist)";
      process.stderr.write(
        `\naihi-code: session ${lastSessionId} closed.\n` +
          `  Resume it with: aihi-code --session ${lastSessionId}${store}\n`,
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
