import * as process from "node:process";
import { RpcClient } from "./rpc/client.js";
import { runTui } from "./tui/index.js";

interface CliOptions {
  cwd: string;
  storePath?: string;
  configPath?: string;
  provider: string;
  model: string;
}

function parseArgs(argv: string[]): CliOptions {
  const options: CliOptions = {
    cwd: process.cwd(),
    storePath: process.env.AIHI_CODE_AGENT_STORE,
    configPath: process.env.AIHI_CODE_AGENT_CONFIG,
    provider: process.env.AIHI_CODE_AGENT_PROVIDER ?? "fake",
    model: process.env.AIHI_CODE_AGENT_MODEL ?? "demo",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--cwd" || argument === "--store" || argument === "--config" || argument === "--provider" || argument === "--model") {
      const value = argv[index + 1];
      if (!value) throw new Error(`${argument} requires a value`);
      if (argument === "--cwd") options.cwd = value;
      if (argument === "--store") options.storePath = value;
      if (argument === "--config") options.configPath = value;
      if (argument === "--provider") options.provider = value;
      if (argument === "--model") options.model = value;
      index += 1;
      continue;
    }
    if (argument === "--help" || argument === "-h") {
      process.stdout.write(
        "Usage: aihi-code [--cwd PATH] [--store PATH] [--config PATH] [--provider NAME] [--model NAME]\n",
      );
      process.exit(0);
    }
    throw new Error(`Unknown argument: ${argument}`);
  }
  return options;
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  const options = parseArgs(argv);
  const client = await RpcClient.connect({
    cwd: options.cwd,
    storePath: options.storePath,
    configPath: options.configPath,
  });
  try {
    await runTui({
      client,
      cwd: options.cwd,
      provider: options.provider,
      model: options.model,
    });
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
