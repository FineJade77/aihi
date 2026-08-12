import { resolve } from "node:path";
import type { ConfigDescriptor, SessionDescriptor } from "@aihi/code-protocol";
import type { RpcClient, SessionCreateParams } from "./rpc/client.js";

export interface BootstrapOptions {
  cwd: string;
  sessionId?: string;
  continueSession?: boolean;
  provider?: string;
  model?: string;
}

export interface BootstrapResult {
  cwd: string;
  config: ConfigDescriptor;
  session: SessionDescriptor;
  provider: string;
  model: string;
  resumed: boolean;
}

type BootstrapClient = Pick<
  RpcClient,
  "createSession" | "getConfig" | "getSession" | "listSessions"
>;

function metadataText(session: SessionDescriptor, key: string): string | undefined {
  const value = session.metadata[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function belongsToWorkspace(session: SessionDescriptor, cwd: string): boolean {
  const sessionCwd = metadataText(session, "cwd");
  return sessionCwd !== undefined && resolve(sessionCwd) === cwd;
}

/** Resolve config and Session before Ink mounts, so the composer never sees placeholders. */
export async function bootstrapSession(
  client: BootstrapClient,
  options: BootstrapOptions,
): Promise<BootstrapResult> {
  const cwd = resolve(options.cwd);
  const config = await client.getConfig(cwd);
  let session: SessionDescriptor | undefined;
  let resumed = false;

  if (options.sessionId !== undefined) {
    session = await client.getSession(options.sessionId);
    if (!belongsToWorkspace(session, cwd)) {
      throw new Error(`Session ${options.sessionId} belongs to another workspace`);
    }
    resumed = true;
  } else if (options.continueSession) {
    const sessions = await client.listSessions(100);
    session = sessions.find((candidate) => belongsToWorkspace(candidate, cwd));
    resumed = session !== undefined;
  }

  if (session === undefined) {
    const create: SessionCreateParams = { cwd };
    if (options.provider !== undefined) create.provider = options.provider;
    if (options.model !== undefined) create.model = options.model;
    session = await client.createSession(create);
  }

  const sessionProvider = metadataText(session, "provider") ?? config.provider.name;
  const requestedProvider = options.provider?.replace(/-/g, "_").toLowerCase();
  const provider = requestedProvider ?? sessionProvider;
  const profile = config.providers.find((candidate) => candidate.name === provider);
  if (profile === undefined) {
    throw new Error(`Provider is not configured: ${provider}`);
  }
  const model =
    options.model ??
    (requestedProvider !== undefined ? profile.model : metadataText(session, "model")) ??
    profile.model;

  return { cwd, config, session, provider, model, resumed };
}
